import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

import backup_google_drive
import server


class GoogleDriveBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database_path = server.DATABASE_PATH
        cls.temp_directory = tempfile.TemporaryDirectory()
        server.DATABASE_PATH = Path(cls.temp_directory.name) / "test.sqlite3"
        server.initialize_database()
        server.create_user("backup.test", "Backup Test", "admin", "Backup-Test-42!")
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

    def test_sqlite_backup_contains_consistent_database_and_storage_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "control_horario.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample (value) VALUES ('persistente')")
                connection.commit()

            archive_document = root / "archivo_documental" / "contrato.pdf"
            archive_document.parent.mkdir()
            archive_document.write_bytes(b"%PDF-1.7\ncontrato")

            backup_path = root / "backup.zip"
            backup_google_drive.create_backup_archive(backup_path, database_path)

            with zipfile.ZipFile(backup_path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "database/control_horario.sqlite3",
                        "storage/archivo_documental/contrato.pdf",
                    },
                )
                restored_path = root / "restored.sqlite3"
                restored_path.write_bytes(archive.read("database/control_horario.sqlite3"))
                self.assertEqual(archive.read("storage/archivo_documental/contrato.pdf"), b"%PDF-1.7\ncontrato")
            with closing(sqlite3.connect(restored_path)) as restored:
                self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "persistente")

    def test_postgres_dump_keeps_credentials_out_of_command_arguments(self):
        database_url = "postgresql://backup-user:secret-password@db.example:5432/company?sslmode=require"
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "database.dump"
            completed = SimpleNamespace(returncode=0, stderr="")
            with (
                patch.object(backup_google_drive.shutil, "which", return_value="pg_dump"),
                patch.object(backup_google_drive.subprocess, "run", return_value=completed) as run,
            ):
                backup_google_drive.create_postgres_dump(database_url, destination)

        args, kwargs = run.call_args
        self.assertNotIn("secret-password", " ".join(args[0]))
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "secret-password")
        self.assertEqual(kwargs["env"]["PGSSLMODE"], "require")
        self.assertIn("--format=custom", args[0])

    def test_drive_upload_creates_backup_folder_and_applies_retention(self):
        class FakeRequest:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class FakeFiles:
            def __init__(self):
                self.list_count = 0
                self.create_count = 0
                self.deleted = []

            def list(self, **kwargs):
                self.list_count += 1
                if self.list_count == 1:
                    return FakeRequest({"files": []})
                return FakeRequest({
                    "files": [
                        {"id": "new", "name": "grupo-ada-backup-current.zip"},
                        {"id": "old-1", "name": "grupo-ada-backup-old-1.zip"},
                        {"id": "old-2", "name": "grupo-ada-backup-old-2.zip"},
                    ]
                })

            def create(self, **kwargs):
                self.create_count += 1
                if self.create_count == 1:
                    return FakeRequest({"id": "backup-folder"})
                return FakeRequest({"id": "new", "name": "grupo-ada-backup-current.zip"})

            def delete(self, fileId):
                self.deleted.append(fileId)
                return FakeRequest({})

        fake_files = FakeFiles()
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "backup.zip"
            archive_path.write_bytes(b"backup")
            with (
                patch.dict(
                    "os.environ",
                    {
                        "GOOGLE_DRIVE_CLIENT_ID": "client-id",
                        "GOOGLE_DRIVE_CLIENT_SECRET": "client-secret",
                        "GOOGLE_DRIVE_REFRESH_TOKEN": "refresh-token",
                    },
                ),
                patch(
                    "googleapiclient.discovery.build",
                    return_value=SimpleNamespace(files=lambda: fake_files),
                ),
            ):
                uploaded = backup_google_drive.upload_backup_to_drive(
                    archive_path,
                    "grupo-ada-backup-current.zip",
                    keep_backups=1,
                )

        self.assertEqual(uploaded["id"], "new")
        self.assertEqual(fake_files.create_count, 2)
        self.assertEqual(fake_files.deleted, ["old-1", "old-2"])

    def test_backup_trigger_requires_secret_and_reports_async_completion(self):
        original_token = server.GOOGLE_DRIVE_BACKUP_TOKEN
        with server.GOOGLE_DRIVE_BACKUP_LOCK:
            original_state = dict(server.GOOGLE_DRIVE_BACKUP_STATE)
            server.GOOGLE_DRIVE_BACKUP_STATE.update(
                backup_id=None,
                status="idle",
                name=None,
                error=None,
            )
        try:
            self.login_pin("administrador", "7804")
            response, _ = self.request(
                "/api/internal/backups/google-drive",
                {},
            )
            self.assertEqual(response.status, 503)

            server.GOOGLE_DRIVE_BACKUP_TOKEN = "test-backup-token"
            response, _ = self.request(
                "/api/internal/backups/google-drive",
                {},
                headers={"Authorization": "Bearer wrong-token"},
            )
            self.assertEqual(response.status, 401)

            with patch.object(
                server.backup_google_drive,
                "run_backup",
                return_value={"name": "grupo-ada-backup-test.zip"},
            ):
                response, started = self.request(
                    "/api/internal/backups/google-drive",
                    {},
                    headers={"Authorization": "Bearer test-backup-token"},
                )
                self.assertEqual(response.status, 202)

                status_response = None
                status = None
                for _ in range(50):
                    status_response, status = self.request(
                        f"/api/internal/backups/google-drive/{started['backupId']}",
                        headers={"Authorization": "Bearer test-backup-token"},
                    )
                    if status["status"] != "running":
                        break
                    server.time.sleep(0.01)
                self.assertEqual(status_response.status, 200)
                self.assertEqual(status["status"], "succeeded")
                self.assertEqual(status["name"], "grupo-ada-backup-test.zip")
        finally:
            server.GOOGLE_DRIVE_BACKUP_TOKEN = original_token
            with server.GOOGLE_DRIVE_BACKUP_LOCK:
                server.GOOGLE_DRIVE_BACKUP_STATE.update(original_state)

    def login_pin(self, username, pin):
        response, result = self.request("/api/login", {"username": username, "pin": pin})
        self.assertEqual(response.status, 200)
        return result["user"]

    def setUp(self):
        from http.cookiejar import CookieJar
        from urllib.request import HTTPCookieProcessor, build_opener

        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"} if data is not None else {}
        request_headers.update(headers or {})
        request = Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method="POST" if data is not None else "GET",
        )
        try:
            response = self.opener.open(request)
        except HTTPError as error:
            response = error
        result = json.loads(response.read())
        response.close()
        return response, result


if __name__ == "__main__":
    unittest.main()
