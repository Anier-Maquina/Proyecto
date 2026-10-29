import base64
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.server import ThreadingHTTPServer


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class AttendanceServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database_path = server.DATABASE_PATH
        cls.temp_directory = tempfile.TemporaryDirectory()
        server.DATABASE_PATH = Path(cls.temp_directory.name) / "test.sqlite3"
        server.initialize_database()
        server.create_user("admin.test", "Admin Test", "admin", "Correct-Horse-42!")
        server.create_user("worker.test", "Worker Test", "worker", "Correct-Horse-43!")
        cls.http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AttendanceHandler)
        cls.server_thread = threading.Thread(target=cls.http_server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.http_server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_server.server_close()
        cls.server_thread.join(timeout=5)
        server.DATABASE_PATH = cls.original_database_path
        cls.temp_directory.cleanup()

    def setUp(self):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        with server.LOGIN_FAILURES_LOCK:
            server.LOGIN_FAILURES.pop("127.0.0.1", None)
        with server.database() as connection:
            connection.execute(
                """
                UPDATE admin_login_state SET failed_attempts = 0, locked = 0
                WHERE user_id = (
                    SELECT id FROM users WHERE username = 'administrador' AND role = 'admin'
                )
                """
            )
            connection.execute("DELETE FROM admin_login_attempts")
            connection.execute("DELETE FROM admin_blocked_ips")

    def request(self, path, payload=None, method=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            response = self.opener.open(request)
        except HTTPError as error:
            response = error
        content_type = response.headers.get("Content-Type", "")
        result = json.loads(response.read()) if "application/json" in content_type else None
        response.close()
        return response, result

    def login_pin(self, username, pin):
        response, result = self.request("/api/login", {"username": username, "pin": pin})
        self.assertEqual(response.status, 200)
        return result["user"]

    def test_ping_and_health_are_public_lightweight_routes(self):
        for path in ("/ping", "/health"):
            with self.subTest(path=path):
                response, payload = self.request(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    payload,
                    {"status": "online", "message": "El servidor de Grupo ADA está online."},
                )

    def test_cors_allows_configured_frontend_and_preflight(self):
        request = Request(
            f"{self.base_url}/api/session",
            headers={"Origin": "https://serviciosgrupoada.es"},
            method="OPTIONS",
        )
        response = self.opener.open(request)
        self.assertEqual(response.status, 204)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "https://serviciosgrupoada.es",
        )
        self.assertEqual(response.headers["Access-Control-Allow-Credentials"], "true")
        self.assertIn("POST", response.headers["Access-Control-Allow-Methods"])
        response.close()

    def test_starter_workers_admin_pin_and_name_management(self):
        self.login_pin("administrador", "7804")
        response, payload = self.request("/api/admin/workers")
        self.assertEqual(response.status, 200)
        workers = payload["workers"]
        self.assertEqual(len(workers), 30)
        self.assertEqual(len({worker["pin"] for worker in workers}), 30)
        self.assertEqual(
            {department: sum(worker["department"] == department for worker in workers) for department in (
                "limpieza", "conserjería", "jardinería"
            )},
            {"limpieza": 10, "conserjería": 10, "jardinería": 10},
        )

        response, _ = self.request("/api/login", {"username": "jardinería", "pin": "4101"})
        self.assertEqual(response.status, 401)
        worker = self.login_pin("Operario 01", "4101")
        self.assertEqual(worker["role"], "worker")
        self.assertEqual(worker["department"], "limpieza")
        self.assertEqual(worker["display_name"], "Operario 01")
        response, _ = self.request("/api/admin/workers")
        self.assertEqual(response.status, 403)

        unauthorized_update = Request(
            f"{self.base_url}/api/admin/workers/{worker['id']}",
            data=json.dumps({"first_name": "Lucía", "last_name": "García"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            self.opener.open(unauthorized_update)
        except HTTPError as error:
            self.assertEqual(error.code, 403)
            error.close()
        else:
            self.fail("Un operario no debe poder editar los datos de otros operarios.")

        admin_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        request = Request(
            f"{self.base_url}/api/admin/workers/{next(item['id'] for item in workers if item['pin'] == '4101')}",
            data=json.dumps({"first_name": "Lucía", "last_name": "García"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        response = self.opener.open(request)
        saved = json.loads(response.read())
        response.close()
        self.assertEqual(saved["worker"]["display_name"], "Lucía García")
        self.assertEqual(saved["worker"]["pin"], "4101")
        self.opener = admin_opener

        response, _ = self.request(
            "/api/attendance/start",
            {
                "latitude": 40.4168,
                "longitude": -3.7038,
                "accuracy": 7,
                "locationTimestamp": "2026-10-24T18:31:06.521Z",
            },
        )
        self.assertEqual(response.status, 201)

        admin_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        response, active = self.request("/api/admin/active")
        self.assertEqual(response.status, 200)
        self.assertEqual(active["workers"][0]["name"], "Lucía García")
        self.assertEqual(active["workers"][0]["department"], "limpieza")
        self.opener = admin_opener

    def test_login_requires_exact_admin_pin_and_limits_failures(self):
        response, _ = self.request("/api/login", {"username": "Administrador", "pin": "7804"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("/api/login", {"username": "operario 01", "pin": "4101"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("/api/login", {"username": "Operario 01", "password": "4101"})
        self.assertEqual(response.status, 400)
        response, _ = self.request("/api/login", {"username": "administrador", "pin": "0000"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("/api/login", {"username": "administrador", "pin": "780"})
        self.assertEqual(response.status, 401)
        for attempt in range(server.LOGIN_FAILURE_LIMIT - 2):
            response, result = self.request("/api/login", {"username": "administrador", "pin": "0000"})
            self.assertEqual(response.status, 423 if attempt == server.LOGIN_FAILURE_LIMIT - 3 else 401)
        response, _ = self.request("/api/login", {"username": "administrador", "pin": "7804"})
        self.assertEqual(response.status, 423)

    def test_admin_login_failures_are_recorded_with_ip_and_type(self):
        for pin in ("12", "0000"):
            response, _ = self.request("/api/login", {"username": "administrador", "pin": pin})
            self.assertEqual(response.status, 401)

        with server.database() as connection:
            attempts = connection.execute(
                """
                SELECT attempted_at, ip_address, failure_type
                FROM admin_login_attempts
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempt["attempted_at"] for attempt in attempts))
        self.assertTrue(all(attempt["ip_address"] == "127.0.0.1" for attempt in attempts))
        self.assertEqual(
            [attempt["failure_type"] for attempt in attempts],
            ["PIN con formato no válido", "PIN incorrecto"],
        )

    def test_ip_blocks_admin_only_and_requires_admin(self):
        response, _ = self.request("/api/admin/security/login-attempts")
        self.assertEqual(response.status, 401)
        response, _ = self.request(
            "/api/admin/security/blocked-ips",
            {"action": "block", "ip": "127.0.0.1"},
        )
        self.assertEqual(response.status, 401)

        worker = self.login_pin("Operario 01", "4101")
        self.assertEqual(worker["role"], "worker")
        response, _ = self.request("/api/admin/security/login-attempts")
        self.assertEqual(response.status, 403)
        response, _ = self.request(
            "/api/admin/security/blocked-ips",
            {"action": "block", "ip": "127.0.0.1"},
        )
        self.assertEqual(response.status, 403)

        worker_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        response, blocked = self.request(
            "/api/admin/security/blocked-ips",
            {"action": "block", "ip": "127.0.0.1"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(blocked, {"ok": True, "ip": "127.0.0.1", "blocked": True})
        admin_opener = self.opener

        response, payload = self.request("/api/admin/security/login-attempts")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["blockedIps"][0]["ip_address"], "127.0.0.1")

        response, _ = self.request("/api/login", {"username": "administrador", "pin": "7804"})
        self.assertEqual(response.status, 423)
        response, _ = self.request(
            "/api/admin/security/blocked-ips",
            {"action": "block", "ip": "not-an-ip"},
        )
        self.assertEqual(response.status, 400)

        self.opener = worker_opener
        response, worker = self.request("/api/login", {"username": "Operario 01", "pin": "4101"})
        self.assertEqual(response.status, 200)
        self.assertEqual(worker["user"]["role"], "worker")

        self.opener = admin_opener
        response, unblocked = self.request(
            "/api/admin/security/blocked-ips",
            {"action": "unblock", "ip": "127.0.0.1"},
        )
        self.assertEqual(response.status, 200)
        self.assertFalse(unblocked["blocked"])
        response, _ = self.request("/api/login", {"username": "administrador", "pin": "7804"})
        self.assertEqual(response.status, 200)
        self.opener = worker_opener

    def test_signing_documents_are_admin_only_and_keep_signed_pdf_on_client(self):
        response, _ = self.request("/api/admin/signing/documents")
        self.assertEqual(response.status, 401)

        self.login_pin("Operario 02", "4102")
        response, _ = self.request("/api/admin/signing/documents")
        self.assertEqual(response.status, 403)

        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        original_pdf = b"%PDF-1.7\ncompany document\n%%EOF"
        response, created = self.request(
            "/api/admin/signing/documents",
            {
                "filename": r"..\Contratos\Documento.pdf",
                "content": base64.b64encode(original_pdf).decode("ascii"),
            },
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(created["filename"], "Documento.pdf")

        response, listing = self.request("/api/admin/signing/documents")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(listing["documents"]), 1)
        document = listing["documents"][0]
        self.assertEqual(document["status"], "pending")
        self.assertEqual(document["sha256"], hashlib.sha256(original_pdf).hexdigest())
        self.assertNotIn("signed_by", document)

        admin_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("Operario 02", "4102")
        response, _ = self.request(f"/api/admin/signing/documents/{created['id']}/signed", {"sha256": "a" * 64})
        self.assertEqual(response.status, 403)
        content_request = Request(
            f"{self.base_url}/api/admin/signing/documents/{created['id']}/content"
        )
        try:
            self.opener.open(content_request)
        except HTTPError as error:
            self.assertEqual(error.code, 403)
            error.close()
        else:
            self.fail("Los operarios no deben poder acceder a documentos de firma del administrador.")

        self.opener = admin_opener
        content_request = Request(
            f"{self.base_url}/api/admin/signing/documents/{created['id']}/content"
        )
        content_response = self.opener.open(content_request)
        self.assertEqual(content_response.read(), original_pdf)
        content_response.close()

        response, _ = self.request(
            f"/api/admin/signing/documents/{created['id']}/signed",
            {"sha256": "invalid"},
        )
        self.assertEqual(response.status, 400)
        signed_hash = "a" * 64
        response, marked = self.request(
            f"/api/admin/signing/documents/{created['id']}/signed",
            {"sha256": signed_hash},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(marked["signed_sha256"], signed_hash)

        response, listing = self.request("/api/admin/signing/documents")
        self.assertEqual(listing["documents"][0]["status"], "signed")
        self.assertEqual(listing["documents"][0]["signed_sha256"], signed_hash)
        self.assertNotIn("signed_pdf", listing["documents"][0])

        response, _ = self.request(
            "/api/admin/signing/documents",
            {"filename": "documento.txt", "content": base64.b64encode(original_pdf).decode("ascii")},
        )
        self.assertEqual(response.status, 400)

    def test_admin_account_locks_after_five_failures_and_manual_command_unlocks(self):
        self.login_pin("administrador", "7804")
        for attempt in range(server.LOGIN_FAILURE_LIMIT):
            response, _ = self.request("/api/login", {"username": "administrador", "pin": "0000"})
            self.assertEqual(response.status, 423 if attempt == server.LOGIN_FAILURE_LIMIT - 1 else 401)

        response, _ = self.request("/api/login", {"username": "administrador", "pin": "7804"})
        self.assertEqual(response.status, 423)
        response, _ = self.request("/api/admin/workers")
        self.assertEqual(response.status, 401)

        response, worker = self.request("/api/login", {"username": "Operario 01", "pin": "4101"})
        self.assertEqual(response.status, 200)
        self.assertEqual(worker["user"]["role"], "worker")

        original_argv = sys.argv
        try:
            sys.argv = ["server.py", "unlock-admin"]
            server.main()
        finally:
            sys.argv = original_argv

        response, admin = self.request("/api/login", {"username": "administrador", "pin": "7804"})
        self.assertEqual(response.status, 200)
        self.assertEqual(admin["user"]["role"], "admin")

    def test_existing_database_is_migrated_without_losing_users(self):
        original_database_path = server.DATABASE_PATH
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.DATABASE_PATH = Path(directory) / "legacy.sqlite3"
                import sqlite3

                connection = sqlite3.connect(server.DATABASE_PATH)
                try:
                    connection.executescript(
                        """
                        CREATE TABLE users (
                            id INTEGER PRIMARY KEY,
                            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                            display_name TEXT NOT NULL,
                            password_hash TEXT NOT NULL,
                            role TEXT NOT NULL CHECK (role IN ('admin', 'worker')),
                            created_at TEXT NOT NULL
                        );
                        CREATE TABLE sessions (
                            token_hash TEXT PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            expires_at TEXT NOT NULL
                        );
                        CREATE TABLE attendance_events (
                            id INTEGER PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES users(id),
                            event_type TEXT NOT NULL CHECK (event_type IN ('start', 'stop')),
                            recorded_at TEXT NOT NULL,
                            latitude REAL NOT NULL,
                            longitude REAL NOT NULL,
                            accuracy REAL NOT NULL,
                            location_timestamp TEXT NOT NULL
                        );
                        """
                    )
                    connection.execute(
                        "INSERT INTO users VALUES (1, 'legacy.worker', 'Legacy Worker', ?, 'worker', ?)",
                        (server.hash_password("Legacy-password-42!"), server.utc_now()),
                    )
                    connection.commit()
                finally:
                    connection.close()
                server.initialize_database()
                with server.database() as connection:
                    legacy_user = connection.execute(
                        "SELECT display_name, role FROM users WHERE username = 'legacy.worker'"
                    ).fetchone()
                    starter_count = connection.execute(
                        "SELECT COUNT(*) AS count FROM users WHERE role = 'worker' AND pin IS NOT NULL"
                    ).fetchone()["count"]
                    migrated_columns = {
                        row["name"] for row in connection.execute("PRAGMA table_info(attendance_events)")
                    }
                    admin_state = connection.execute(
                        "SELECT failed_attempts, locked FROM admin_login_state "
                        "WHERE user_id = (SELECT id FROM users WHERE username = 'administrador')"
                    ).fetchone()
                self.assertEqual(dict(legacy_user), {"display_name": "Legacy Worker", "role": "worker"})
                self.assertEqual(starter_count, 30)
                self.assertIn("worker_name", migrated_columns)
                self.assertIn("department", migrated_columns)
                self.assertIn("location_available", migrated_columns)
                self.assertIn("incident", migrated_columns)
                self.assertEqual(dict(admin_state), {"failed_attempts": 0, "locked": 0})
        finally:
            server.DATABASE_PATH = original_database_path

    def test_role_guards_and_protected_routes(self):
        response, _ = self.request("/api/admin/active")
        self.assertEqual(response.status, 401)
        response, _ = self.request("/api/admin/attendance-reports")
        self.assertEqual(response.status, 401)
        response, _ = self.request("/api/admin/monthly-finances")
        self.assertEqual(response.status, 401)
        response, _ = self.request("/api/admin/attendance-reports/1", {}, method="PUT")
        self.assertEqual(response.status, 401)

        user = self.login_pin("Operario 10", "4110")
        self.assertEqual(user["role"], "worker")
        response, _ = self.request("/api/admin/active")
        self.assertEqual(response.status, 403)
        response, _ = self.request(
            "/api/admin/monthly-finances",
            {"month": "2026-10", "incomes": {"communities": []}, "expenses": {}},
        )
        self.assertEqual(response.status, 403)
        response, _ = self.request(
            "/api/admin/attendance-reports/1",
            {
                "worker": "Worker Test",
                "date": "2026-10-24",
                "startTime": "08:00:00",
                "stopTime": "17:00:00",
            },
            method="PUT",
        )
        self.assertEqual(response.status, 403)

        response, _ = self.request("/index.html?select=1")
        self.assertEqual(response.status, 200)
        self.assertEqual(urlparse(response.url).path, "/index.html")

        response, _ = self.request("/admin/dashboard.html")
        self.assertEqual(response.status, 200)
        self.assertEqual(urlparse(response.url).path, "/worker/fichar.html")

        response, _ = self.request("/Almacenamiento/test.sqlite3")
        self.assertEqual(response.status, 404)
        response, _ = self.request("/server.py")
        self.assertEqual(response.status, 404)
        try:
            response = self.opener.open(Request(self.base_url + "/Almacenamiento/test.sqlite3", method="HEAD"))
        except HTTPError as error:
            response = error
        self.assertEqual(response.status, 405)
        response.close()

    def test_attendance_start_stop_and_admin_map_data(self):
        self.login_pin("Operario 02", "4102")
        location = {
            "latitude": 40.4168,
            "longitude": -3.7038,
            "accuracy": 8.5,
            "locationTimestamp": "2026-10-24T18:31:06.521Z",
        }
        response, start_result = self.request("/api/attendance/start", location)
        self.assertEqual(response.status, 201)
        self.assertEqual(start_result["event"], "start")
        self.assertTrue(start_result["recordedAt"].endswith("+00:00"))
        report_directory = server.DATABASE_PATH.parent / "control de fichaje"
        open_reports = [
            report
            for report in report_directory.glob("*_control_de_fichaje_*.json")
            if json.loads(report.read_text(encoding="utf-8"))["worker"] == "Operario 02"
        ]
        self.assertEqual(len(open_reports), 1)
        self.assertIsNone(json.loads(open_reports[0].read_text(encoding="utf-8"))["shifts"][0]["stoppedAt"])

        admin_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        response, active_result = self.request("/api/admin/active")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(active_result["workers"]), 1)
        self.assertEqual(active_result["workers"][0]["name"], "Operario 02")
        self.assertEqual(active_result["workers"][0]["latitude"], location["latitude"])
        response, reports_result = self.request("/api/admin/attendance-reports")
        self.assertEqual(response.status, 200)
        open_shift = next(shift for shift in reports_result["shifts"] if shift["worker"] == "Operario 02")
        self.assertIsNone(open_shift["stoppedAt"])
        self.opener = admin_opener

        response, _ = self.request("/api/attendance/start", location)
        self.assertEqual(response.status, 409)
        response, stop_result = self.request("/api/attendance/stop", location)
        self.assertEqual(response.status, 201)
        self.assertEqual(stop_result["event"], "stop")
        self.assertEqual(stop_result["shift"]["worker"], "Operario 02")
        self.assertEqual(stop_result["shift"]["date"], stop_result["recordedAt"][:10])
        self.assertEqual(stop_result["shift"]["stoppedAt"], stop_result["recordedAt"])
        reports = [
            report
            for report in report_directory.glob("*_control_de_fichaje_*.html")
            if "Operario 02" in report.read_text(encoding="utf-8")
        ]
        self.assertEqual(len(reports), 1)
        self.assertEqual(stop_result["report"], reports[0].name)
        report_content = reports[0].read_text(encoding="utf-8")
        self.assertIn("Operario 02", report_content)
        self.assertIn("Entrada (UTC)", report_content)
        self.assertIn("Salida (UTC)", report_content)
        self.assertIn("Incidencia", report_content)
        json_reports = [
            report
            for report in report_directory.glob("*_control_de_fichaje_*.json")
            if json.loads(report.read_text(encoding="utf-8"))["worker"] == "Operario 02"
        ]
        self.assertEqual(len(json_reports), 1)
        structured_report = json.loads(json_reports[0].read_text(encoding="utf-8"))
        self.assertEqual(structured_report["worker"], "Operario 02")
        self.assertEqual(len(structured_report["shifts"]), 1)

        admin_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        response, active_result = self.request("/api/admin/active")
        self.assertEqual(response.status, 200)
        self.assertEqual(active_result["workers"], [])
        response, reports_result = self.request("/api/admin/attendance-reports")
        self.assertEqual(response.status, 200)
        saved_shift = next(shift for shift in reports_result["shifts"] if shift["worker"] == "Operario 02")
        self.assertIn(
            {"id": saved_shift["userId"], "name": "Operario 02"},
            reports_result["workers"],
        )
        response, update_result = self.request(
            f"/api/admin/attendance-reports/{saved_shift['id']}",
            {
                "worker": "Operario corregido",
                "date": "2026-10-23",
                "startTime": "18:00:00",
                "stopTime": "19:00:00",
                "incident": "Corrección manual",
            },
            method="PUT",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(update_result["shift"]["worker"], "Operario corregido")
        self.assertEqual(update_result["shift"]["date"], "2026-10-23")
        self.assertEqual(update_result["shift"]["startedAt"], "2026-10-23T18:00:00+00:00")
        self.assertEqual(update_result["shift"]["stoppedAt"], "2026-10-23T19:00:00+00:00")
        self.assertEqual(update_result["shift"]["incident"], "Corrección manual")
        response, reports_result = self.request("/api/admin/attendance-reports")
        self.assertEqual(response.status, 200)
        corrected_shift = next(shift for shift in reports_result["shifts"] if shift["worker"] == "Operario corregido")
        self.assertEqual(corrected_shift["date"], "2026-10-23")
        structured_report = json.loads(json_reports[0].read_text(encoding="utf-8"))
        self.assertEqual(structured_report["shifts"][0]["worker"], "Operario corregido")
        self.assertEqual(structured_report["shifts"][0]["date"], "2026-10-23")
        self.assertEqual(structured_report["shifts"][0]["incident"], "Corrección manual")
        self.opener = admin_opener

        response, worker_result = self.request("/api/attendance/mine")
        self.assertEqual(response.status, 200)
        self.assertFalse(worker_result["active"])
        self.assertEqual([event["event_type"] for event in worker_result["events"]], ["stop", "start"])

        response, _ = self.request("/api/attendance/start", location)
        self.assertEqual(response.status, 201)
        response, _ = self.request("/api/attendance/stop", location)
        self.assertEqual(response.status, 201)
        updated_report = reports[0].read_text(encoding="utf-8")
        self.assertEqual(updated_report.count("<tr>"), 3)

        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        response, report_result = self.request("/api/admin/attendance-reports")
        self.assertEqual(response.status, 200)
        report = next(item for item in report_result["reports"] if item["filename"] == reports[0].name)
        self.assertEqual(report["content"], updated_report)

    def test_attendance_can_be_recorded_without_precise_location(self):
        self.login_pin("Operario 03", "4103")
        unavailable_location = {}
        response, start_result = self.request("/api/attendance/start", unavailable_location)
        self.assertEqual(response.status, 201)
        self.assertFalse(start_result["locationAvailable"])

        admin_opener = self.opener
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.login_pin("administrador", "7804")
        response, active_result = self.request("/api/admin/active")
        self.assertEqual(response.status, 200)
        self.assertNotIn("Operario 03", {worker["name"] for worker in active_result["workers"]})
        self.opener = admin_opener

        imprecise_location = {
            "latitude": "desconocida",
            "longitude": None,
            "accuracy": -1,
            "locationTimestamp": "no es una fecha",
        }
        response, stop_result = self.request("/api/attendance/stop", imprecise_location)
        self.assertEqual(response.status, 201)
        self.assertFalse(stop_result["locationAvailable"])
        self.assertEqual(stop_result["shift"]["worker"], "Operario 03")
        response, attendance = self.request("/api/attendance/mine")
        self.assertEqual(response.status, 200)
        self.assertFalse(attendance["active"])
        self.assertFalse(attendance["events"][0]["location_available"])
        self.assertIsNone(attendance["events"][0]["latitude"])
        report_directory = server.DATABASE_PATH.parent / "control de fichaje"
        reports = list(report_directory.glob("*_control_de_fichaje_*.html"))
        self.assertTrue(any("Operario 03" in report.read_text(encoding="utf-8") for report in reports))

    def test_monthly_finances_are_validated_and_saved_as_json(self):
        self.login_pin("administrador", "7804")
        payload = {
            "month": "2026-10",
            "incomes": {
                "communities": [
                    {"name": "Comunidad Uno", "amount": 10000},
                    {"name": "Comunidad Dos", "amount": 2500.5},
                ]
            },
            "expenses": {
                "personnel": {
                    "payroll": 4000,
                    "employer_social_security": 900,
                    "accident_insurance": 100,
                    "occupational_risk_prevention": 50,
                },
                "taxes": {
                    "quarterly_vat": 1000,
                    "quarterly_income_tax": 250,
                    "annual_corporate_tax": 0,
                },
                "operations": {
                    "cleaning_supplies": 300,
                    "machinery_repairs": 200,
                    "workwear_ppe": 100,
                    "transport_fuel": 150,
                    "management": 120,
                    "office_telecom_misc": 80,
                },
            },
        }
        response, result = self.request("/api/admin/monthly-finances", payload)
        self.assertEqual(response.status, 201)
        record = result["record"]
        self.assertEqual(record["incomes"]["total"], 12500.5)
        self.assertEqual(record["expenses"]["total"], 7250)
        self.assertEqual(record["netProfit"], 5250.5)

        report_directory = server.DATABASE_PATH.parent / "control mensual de gastos e ingresos"
        report_path = report_directory / "2026-10.json"
        self.assertTrue(report_path.is_file())
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), record)

        response, reports = self.request("/api/admin/monthly-finances")
        self.assertEqual(response.status, 200)
        self.assertEqual(reports["reports"][0]["filename"], "2026-10.json")
        self.assertEqual(json.loads(reports["reports"][0]["content"]), record)

        payload["month"] = "2026-13"
        response, error = self.request("/api/admin/monthly-finances", payload)
        self.assertEqual(response.status, 400)
        self.assertIn("mes válido", error["error"])


if __name__ == "__main__":
    unittest.main()
