import argparse
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("ATTENDANCE_DATABASE", ROOT / "Almacenamiento" / "control_horario.sqlite3"))
SESSION_DURATION = timedelta(hours=12)
PBKDF2_ITERATIONS = 310_000
COOKIE_NAME = "control_horario_session"
MAX_REQUEST_BYTES = 16_384
MAX_SIGNING_DOCUMENT_BYTES = 10 * 1024 * 1024
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = 15 * 60
LOGIN_FAILURES = {}
LOGIN_FAILURES_LOCK = threading.Lock()
STARTER_DEPARTMENTS = (
    ("limpieza", 4101),
    ("conserjería", 5201),
    ("jardinería", 6301),
)
FINANCE_EXPENSE_FIELDS = {
    "personnel": (
        "payroll",
        "employer_social_security",
        "accident_insurance",
        "occupational_risk_prevention",
    ),
    "taxes": ("quarterly_vat", "quarterly_income_tax", "annual_corporate_tax"),
    "operations": (
        "cleaning_supplies",
        "machinery_repairs",
        "workwear_ppe",
        "transport_fuel",
        "management",
        "office_telecom_misc",
    ),
}


@contextmanager
def database():
    connection = connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_text_atomically(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def write_json_atomically(path, payload):
    write_text_atomically(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def get_attendance_shifts(connection, user_id):
    events = connection.execute(
        """
        SELECT e.id, e.event_type, e.recorded_at, e.incident,
               COALESCE(NULLIF(e.worker_name, ''), u.display_name) AS worker_name
        FROM attendance_events e
        JOIN users u ON u.id = e.user_id
        WHERE e.user_id = ?
        ORDER BY e.recorded_at, e.id
        """,
        (user_id,),
    ).fetchall()
    shifts = []
    shift_start = None
    for event in events:
        if event["event_type"] == "start":
            shift_start = event
        elif shift_start is not None:
            start_time = datetime.fromisoformat(
                shift_start["recorded_at"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            stop_time = datetime.fromisoformat(
                event["recorded_at"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            shifts.append(
                {
                    "id": shift_start["id"],
                    "stopEventId": event["id"],
                    "userId": user_id,
                    "worker": shift_start["worker_name"],
                    "incident": shift_start["incident"] or "",
                    "date": start_time.strftime("%Y-%m-%d"),
                    "startedAt": start_time.isoformat(timespec="seconds"),
                    "stoppedAt": stop_time.isoformat(timespec="seconds"),
                }
            )
            shift_start = None
    if shift_start is not None:
        start_time = datetime.fromisoformat(
            shift_start["recorded_at"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        shifts.append(
            {
                "id": shift_start["id"],
                "stopEventId": None,
                "userId": user_id,
                "worker": shift_start["worker_name"],
                "incident": shift_start["incident"] or "",
                "date": start_time.strftime("%Y-%m-%d"),
                "startedAt": start_time.isoformat(timespec="seconds"),
                "stoppedAt": None,
            }
        )
    return shifts


def write_attendance_report(connection, user_id):
    worker = connection.execute(
        "SELECT id, display_name FROM users WHERE id = ? AND role = 'worker'",
        (user_id,),
    ).fetchone()
    if not worker:
        return None
    shifts = get_attendance_shifts(connection, user_id)
    if not shifts:
        return None
    report_directory = DATABASE_PATH.parent / "control de fichaje"
    report_directory.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w-]+", "_", worker["display_name"], flags=re.UNICODE).strip("_")
    safe_name = safe_name or f"operario-{worker['id']}"
    filename = f"{safe_name}_control_de_fichaje_{worker['id']}.html"
    report_path = report_directory / filename
    json_filename = f"{safe_name}_control_de_fichaje_{worker['id']}.json"
    rows = []
    for shift in shifts:
        start_time = datetime.fromisoformat(shift["startedAt"])
        stop_time = datetime.fromisoformat(shift["stoppedAt"]) if shift["stoppedAt"] else None
        rows.append(
            "<tr>"
            f"<td>{html.escape(shift['worker'])}</td>"
            f"<td>{html.escape(shift['date'])}</td>"
            f"<td>{html.escape(start_time.strftime('%H:%M:%S UTC'))}</td>"
            f"<td>{html.escape(stop_time.strftime('%H:%M:%S UTC') if stop_time else 'En curso')}</td>"
            f"<td>{html.escape(shift['incident'])}</td>"
            "</tr>"
        )
    report = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Control de fichaje - {html.escape(worker["display_name"])}</title>
<style>
body {{ color: #1e293b; font: 16px Arial, sans-serif; margin: 2rem auto; max-width: 900px; padding: 0 1rem; }}
h1 {{ font-size: 1.5rem; }}
table {{ border-collapse: collapse; margin-top: 1.5rem; width: 100%; }}
th, td {{ border: 1px solid #cbd5e1; padding: .75rem; text-align: left; }}
th {{ background: #f1f5f9; }}
@media print {{ body {{ margin: 0; max-width: none; }} }}
</style>
</head>
<body>
<h1>Control de fichaje</h1>
<p><strong>Trabajador:</strong> {html.escape(worker["display_name"])}</p>
<table>
<thead><tr><th>Trabajador</th><th>Fecha</th><th>Entrada (UTC)</th><th>Salida (UTC)</th><th>Incidencia</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
</body>
</html>
"""
    write_text_atomically(report_path, report)
    write_json_atomically(
        report_directory / json_filename,
        {
            "worker": worker["display_name"],
            "userId": worker["id"],
            "shifts": shifts,
        },
    )

    for old_report in report_directory.glob(f"*_control_de_fichaje_{worker['id']}.html"):
        if old_report != report_path:
            old_report.unlink()
    for old_report in report_directory.glob(f"*_control_de_fichaje_{worker['id']}.json"):
        if old_report.name != json_filename:
            old_report.unlink()
    return {
        "userId": worker["id"],
        "filename": filename,
        "content": report,
        "shifts": shifts,
    }


def update_attendance_shift(connection, start_event_id, payload):
    worker_name = payload.get("worker")
    date_value = payload.get("date")
    start_value = payload.get("startTime")
    stop_value = payload.get("stopTime", "")
    incident = payload.get("incident")
    if not isinstance(worker_name, str) or not worker_name.strip() or len(worker_name.strip()) > 100:
        raise ValueError("Indica un nombre de trabajador válido de hasta 100 caracteres.")
    if not isinstance(date_value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        raise ValueError("Indica una fecha válida.")
    try:
        date.fromisoformat(date_value)
    except ValueError:
        raise ValueError("Indica una fecha válida.") from None
    if not isinstance(start_value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", start_value):
        raise ValueError("Indica una hora de entrada válida.")
    if not isinstance(stop_value, str) or (
        stop_value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", stop_value)
    ):
        raise ValueError("Indica una hora de salida válida.")

    start_time = datetime.fromisoformat(f"{date_value}T{start_value}").replace(tzinfo=timezone.utc)
    stop_time = (
        datetime.fromisoformat(f"{date_value}T{stop_value}").replace(tzinfo=timezone.utc)
        if stop_value
        else None
    )
    if stop_time and stop_time <= start_time:
        raise ValueError("La hora de salida debe ser posterior a la hora de entrada.")

    start_event = connection.execute(
        """
        SELECT id, user_id, event_type, recorded_at, worker_name, department, incident
        FROM attendance_events
        WHERE id = ? AND event_type = 'start'
        """,
        (start_event_id,),
    ).fetchone()
    if not start_event:
        raise LookupError("No se encontró el fichaje que quieres editar.")
    worker_name = worker_name.strip()
    if incident is None:
        incident = start_event["incident"] or ""
    if not isinstance(incident, str) or len(incident.strip()) > 500:
        raise ValueError("La incidencia debe ser un texto de hasta 500 caracteres.")
    incident = incident.strip()
    next_event = connection.execute(
        """
        SELECT id, event_type, recorded_at
        FROM attendance_events
        WHERE user_id = ? AND id > ?
        ORDER BY id
        LIMIT 1
        """,
        (start_event["user_id"], start_event_id),
    ).fetchone()
    stop_event = next_event if next_event and next_event["event_type"] == "stop" else None
    if next_event and not stop_event:
        raise ValueError("No se puede editar un fichaje con eventos posteriores inconsistentes.")
    if stop_event and not stop_time:
        raise ValueError("Indica la hora de salida para completar el registro.")

    previous_event = connection.execute(
        """
        SELECT recorded_at FROM attendance_events
        WHERE user_id = ? AND id < ?
        ORDER BY id DESC LIMIT 1
        """,
        (start_event["user_id"], start_event_id),
    ).fetchone()
    following_event = connection.execute(
        """
        SELECT recorded_at FROM attendance_events
        WHERE user_id = ? AND id > ?
        ORDER BY id LIMIT 1
        """,
        (start_event["user_id"], stop_event["id"] if stop_event else start_event_id),
    ).fetchone()
    start_timestamp = start_time.isoformat(timespec="seconds")
    stop_timestamp = stop_time.isoformat(timespec="seconds") if stop_time else None
    if previous_event and start_time <= datetime.fromisoformat(
        previous_event["recorded_at"].replace("Z", "+00:00")
    ):
        raise ValueError("La entrada debe ser posterior al fichaje anterior.")
    if following_event and stop_time and stop_time >= datetime.fromisoformat(
        following_event["recorded_at"].replace("Z", "+00:00")
    ):
        raise ValueError("La salida debe ser anterior al siguiente fichaje.")

    connection.execute(
        "UPDATE attendance_events SET recorded_at = ?, worker_name = ?, incident = ? WHERE id = ?",
        (start_timestamp, worker_name, incident, start_event_id),
    )
    if stop_event:
        connection.execute(
            "UPDATE attendance_events SET recorded_at = ?, worker_name = ? WHERE id = ?",
            (stop_timestamp, worker_name, stop_event["id"]),
        )
    elif stop_time:
        connection.execute(
            """
            INSERT INTO attendance_events
                (user_id, event_type, recorded_at, latitude, longitude, accuracy, location_timestamp,
                 worker_name, department, location_available)
            VALUES (?, 'stop', ?, 0, 0, 10000, '', ?, ?, 0)
            """,
            (start_event["user_id"], stop_timestamp, worker_name, start_event["department"]),
        )

    report = write_attendance_report(connection, start_event["user_id"])
    updated_shift = next(
        shift for shift in report["shifts"] if shift["id"] == start_event_id
    )
    return updated_shift, report


def normalize_monthly_finance(payload):
    month = payload.get("month")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("Selecciona un mes válido.")

    incomes_payload = payload.get("incomes")
    communities_payload = incomes_payload.get("communities") if isinstance(incomes_payload, dict) else None
    if not isinstance(communities_payload, list) or len(communities_payload) > 100:
        raise ValueError("Los ingresos deben incluir una lista válida de comunidades.")
    communities = []
    for community in communities_payload:
        if not isinstance(community, dict):
            raise ValueError("Hay una comunidad con datos no válidos.")
        name = community.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Cada comunidad debe tener un nombre válido de hasta 100 caracteres.")
        try:
            amount = float(community.get("amount", 0))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("El importe de una comunidad no es válido.") from None
        if not math.isfinite(amount) or amount < 0 or amount > 1_000_000_000_000:
            raise ValueError("Los importes deben ser números no negativos válidos.")
        communities.append({"name": name.strip(), "amount": round(amount, 2)})

    expenses_payload = payload.get("expenses")
    if not isinstance(expenses_payload, dict):
        raise ValueError("Faltan los datos de gastos mensuales.")
    expenses = {}
    for group, field_names in FINANCE_EXPENSE_FIELDS.items():
        submitted_group = expenses_payload.get(group)
        if not isinstance(submitted_group, dict):
            raise ValueError("La estructura de gastos está incompleta.")
        expenses[group] = {}
        for field_name in field_names:
            try:
                amount = float(submitted_group.get(field_name, 0))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Los importes deben ser números no negativos válidos.") from None
            if not math.isfinite(amount) or amount < 0 or amount > 1_000_000_000_000:
                raise ValueError("Los importes deben ser números no negativos válidos.")
            expenses[group][field_name] = round(amount, 2)

    total_income = round(sum(community["amount"] for community in communities), 2)
    expense_totals = {
        group: round(sum(values.values()), 2)
        for group, values in expenses.items()
    }
    total_expenses = round(sum(expense_totals.values()), 2)
    return {
        "month": month,
        "incomes": {"communities": communities, "total": total_income},
        "expenses": {
            **expenses,
            "totals": expense_totals,
            "total": total_expenses,
        },
        "netProfit": round(total_income - total_expenses, 2),
        "savedAt": utc_now(),
    }


def connect_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'worker')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_login_state (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS admin_login_attempts (
                id INTEGER PRIMARY KEY,
                attempted_at TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                failure_type TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS admin_login_attempts_time
                ON admin_login_attempts(attempted_at DESC);
            CREATE TABLE IF NOT EXISTS admin_blocked_ips (
                ip_address TEXT PRIMARY KEY,
                blocked_at TEXT NOT NULL,
                blocked_by INTEGER REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS signing_documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                storage_name TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'signed')),
                signed_sha256 TEXT,
                uploaded_at TEXT NOT NULL,
                signed_at TEXT,
                signed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS attendance_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                event_type TEXT NOT NULL CHECK (event_type IN ('start', 'stop')),
                recorded_at TEXT NOT NULL,
                latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
                longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
                accuracy REAL NOT NULL CHECK (accuracy >= 0),
                location_timestamp TEXT NOT NULL,
                location_available INTEGER NOT NULL DEFAULT 1 CHECK (location_available IN (0, 1)),
                incident TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS attendance_user_time
                ON attendance_events(user_id, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
            """
        )
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        for column, definition in (
            ("first_name", "TEXT NOT NULL DEFAULT ''"),
            ("last_name", "TEXT NOT NULL DEFAULT ''"),
            ("department", "TEXT"),
            ("pin", "TEXT"),
        ):
            if column not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(attendance_events)")}
        for column in ("worker_name", "department"):
            if column not in event_columns:
                connection.execute(
                    f"ALTER TABLE attendance_events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        if "incident" not in event_columns:
            connection.execute(
                "ALTER TABLE attendance_events ADD COLUMN incident TEXT NOT NULL DEFAULT ''"
            )
        if "location_available" not in event_columns:
            connection.execute(
                "ALTER TABLE attendance_events ADD COLUMN location_available INTEGER NOT NULL DEFAULT 1 CHECK (location_available IN (0, 1))"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_pin_unique ON users(pin) WHERE pin IS NOT NULL"
        )
        seed_starter_users(connection)
        admin = connection.execute(
            "SELECT id FROM users WHERE username = 'administrador' AND role = 'admin'"
        ).fetchone()
        if admin:
            connection.execute(
                "INSERT OR IGNORE INTO admin_login_state (user_id) VALUES (?)",
                (admin["id"],),
            )


def admin_login_is_locked():
    with database() as connection:
        row = connection.execute(
            """
            SELECT state.locked
            FROM admin_login_state AS state
            JOIN users ON users.id = state.user_id
            WHERE users.username = 'administrador' AND users.role = 'admin'
            """
        ).fetchone()
    return bool(row and row["locked"])


def is_admin_ip_blocked(ip_address):
    with database() as connection:
        return connection.execute(
            "SELECT 1 FROM admin_blocked_ips WHERE ip_address = ?",
            (ip_address,),
        ).fetchone() is not None


def record_admin_login_event(ip_address, failure_type):
    with database() as connection:
        connection.execute(
            """
            INSERT INTO admin_login_attempts (attempted_at, ip_address, failure_type)
            VALUES (?, ?, ?)
            """,
            (utc_now(), ip_address, failure_type),
        )


def record_admin_login_failure(ip_address, failure_type):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO admin_login_attempts (attempted_at, ip_address, failure_type)
            VALUES (?, ?, ?)
            """,
            (utc_now(), ip_address, failure_type),
        )
        admin = connection.execute(
            "SELECT id FROM users WHERE username = 'administrador' AND role = 'admin'"
        ).fetchone()
        if not admin:
            return True
        connection.execute(
            """
            UPDATE admin_login_state
            SET failed_attempts = failed_attempts + 1,
                locked = CASE WHEN failed_attempts + 1 >= ? THEN 1 ELSE locked END
            WHERE user_id = ?
            """,
            (LOGIN_FAILURE_LIMIT, admin["id"]),
        )
        state = connection.execute(
            "SELECT locked FROM admin_login_state WHERE user_id = ?",
            (admin["id"],),
        ).fetchone()
        locked = bool(state and state["locked"])
        if locked:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (admin["id"],))
    return locked


def reset_admin_login_failures():
    with database() as connection:
        connection.execute(
            """
            UPDATE admin_login_state SET failed_attempts = 0
            WHERE user_id = (
                SELECT id FROM users WHERE username = 'administrador' AND role = 'admin'
            ) AND locked = 0
            """
        )


def unlock_admin_account():
    initialize_database()
    with database() as connection:
        result = connection.execute(
            """
            UPDATE admin_login_state
            SET failed_attempts = 0, locked = 0
            WHERE user_id = (
                SELECT id FROM users WHERE username = 'administrador' AND role = 'admin'
            )
            """
        )
    if result.rowcount == 0:
        raise LookupError("No se encontró la cuenta administrativa.")


def validate_ip_address(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Indica una dirección IP válida.")
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise ValueError("Indica una dirección IPv4 o IPv6 válida.") from None


def seed_starter_users(connection):
    admin = connection.execute(
        "SELECT id, role FROM users WHERE username = 'administrador'"
    ).fetchone()
    if admin and admin["role"] != "admin":
        raise ValueError("El usuario 'administrador' ya existe con un rol incompatible.")
    if admin:
        connection.execute(
            """
            UPDATE users SET display_name = 'Administrador', first_name = 'Administrador',
                last_name = '', pin = '7804', password_hash = ?
            WHERE id = ?
            """,
            (hash_password("7804"), admin["id"]),
        )
    else:
        connection.execute(
            """
            INSERT INTO users
                (username, display_name, password_hash, role, created_at, first_name, last_name, pin)
            VALUES (?, ?, ?, 'admin', ?, ?, ?, ?)
            """,
            ("administrador", "Administrador", hash_password("7804"), utc_now(), "Administrador", "", "7804"),
        )

    for department, first_pin in STARTER_DEPARTMENTS:
        for index in range(10):
            username = f"ada-worker-{first_pin // 1000:01d}-{index + 1:02d}"
            pin = f"{first_pin + index:04d}"
            existing = connection.execute(
                "SELECT id, role, pin FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                if existing["role"] != "worker" or existing["pin"] not in (None, pin):
                    raise ValueError(f"La cuenta inicial '{username}' ya existe con datos incompatibles.")
                if existing["pin"] is None:
                    connection.execute(
                        """
                        UPDATE users SET display_name = ?, password_hash = ?, first_name = ?,
                            last_name = ?, department = ?, pin = ?
                        WHERE id = ?
                        """,
                        (
                            f"Operario {index + 1:02d}",
                            hash_password(pin),
                            "Operario",
                            f"{index + 1:02d}",
                            department,
                            pin,
                            existing["id"],
                        ),
                    )
                continue
            connection.execute(
                """
                INSERT INTO users
                    (username, display_name, password_hash, role, created_at,
                     first_name, last_name, department, pin)
                VALUES (?, ?, ?, 'worker', ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    f"Operario {index + 1:02d}",
                    hash_password(pin),
                    utc_now(),
                    "Operario",
                    f"{index + 1:02d}",
                    department,
                    pin,
                ),
            )


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password, encoded_hash):
    try:
        salt_hex, digest_hex = encoded_hash.split(":", 1)
        candidate = hash_password(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


def create_user(username, display_name, role, password):
    username = username.strip()
    display_name = display_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]{3,120}", username):
        raise ValueError("El usuario debe tener entre 3 y 120 caracteres válidos.")
    if not display_name or len(display_name) > 120:
        raise ValueError("El nombre visible es obligatorio y no puede superar 120 caracteres.")
    if role not in ("admin", "worker"):
        raise ValueError("El rol debe ser admin o worker.")
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("La contraseña debe tener al menos 12 caracteres.")

    with database() as connection:
        connection.execute(
            "INSERT INTO users (username, display_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, display_name, hash_password(password), role, utc_now()),
        )


class AttendanceHandler(SimpleHTTPRequestHandler):
    server_version = "AttendanceServer/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format_string, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format_string % args))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, status, content, content_type, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.end_headers()
        self.wfile.write(content)

    def read_json(self, max_bytes=MAX_REQUEST_BYTES):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Longitud de petición no válida.") from None
        if length <= 0 or length > max_bytes:
            raise ValueError("El cuerpo de la petición está vacío o supera el límite permitido.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("El cuerpo de la petición debe contener JSON válido.") from None
        if not isinstance(payload, dict):
            raise ValueError("El cuerpo de la petición debe ser un objeto JSON.")
        return payload

    def current_user(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(COOKIE_NAME)
        if morsel is None:
            return None
        token_hash = hashlib.sha256(morsel.value.encode("utf-8")).hexdigest()
        with database() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, users.display_name, users.role,
                       users.first_name, users.last_name, users.department
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash, utc_now()),
            ).fetchone()
        return dict(row) if row else None

    def login_is_rate_limited(self):
        now = time.monotonic()
        address = self.client_address[0]
        with LOGIN_FAILURES_LOCK:
            failures = [timestamp for timestamp in LOGIN_FAILURES.get(address, []) if now - timestamp < LOGIN_FAILURE_WINDOW]
            LOGIN_FAILURES[address] = failures
            return len(failures) >= LOGIN_FAILURE_LIMIT

    def record_login_failure(self):
        now = time.monotonic()
        address = self.client_address[0]
        with LOGIN_FAILURES_LOCK:
            failures = [timestamp for timestamp in LOGIN_FAILURES.get(address, []) if now - timestamp < LOGIN_FAILURE_WINDOW]
            failures.append(now)
            LOGIN_FAILURES[address] = failures

    def clear_login_failures(self):
        with LOGIN_FAILURES_LOCK:
            LOGIN_FAILURES.pop(self.client_address[0], None)

    def require_role(self, role):
        user = self.current_user()
        if not user:
            self.send_json(401, {"error": "Inicia sesión para continuar."})
            return None
        if user["role"] != role:
            self.send_json(403, {"error": "No tienes permisos para acceder a este recurso."})
            return None
        return user

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/ping", "/health"):
            self.send_json(200, {"status": "online", "message": "El servidor de Grupo ADA está online."})
            return
        if path == "/api/session":
            user = self.current_user()
            if not user:
                self.send_json(401, {"error": "Sesión no iniciada."})
                return
            self.send_json(200, {"user": user})
            return
        if path == "/api/admin/signing/documents":
            if not self.require_role("admin"):
                return
            try:
                with database() as connection:
                    rows = connection.execute(
                        """
                        SELECT id, filename, sha256, status, signed_sha256, uploaded_at, signed_at
                        FROM signing_documents
                        ORDER BY uploaded_at DESC, filename COLLATE NOCASE
                        """
                    ).fetchall()
            except sqlite3.Error:
                self.send_json(500, {"error": "No se pudieron cargar los documentos de firma."})
                return
            self.send_json(200, {"documents": [dict(row) for row in rows]})
            return
        signing_content_match = re.fullmatch(
            r"/api/admin/signing/documents/([a-f0-9]{32})/content",
            path,
        )
        if signing_content_match:
            if not self.require_role("admin"):
                return
            try:
                with database() as connection:
                    row = connection.execute(
                        "SELECT filename, storage_name FROM signing_documents WHERE id = ?",
                        (signing_content_match.group(1),),
                    ).fetchone()
                if not row:
                    self.send_json(404, {"error": "No se encontró el documento solicitado."})
                    return
                file_path = DATABASE_PATH.parent / "documentos_firma" / row["storage_name"]
                content = file_path.read_bytes()
            except OSError:
                self.send_json(500, {"error": "No se pudo leer el documento original."})
                return
            except sqlite3.Error:
                self.send_json(500, {"error": "No se pudo consultar el documento original."})
                return
            self.send_file(200, content, "application/pdf")
            return
        if path == "/api/admin/attendance-reports":
            if not self.require_role("admin"):
                return
            try:
                with database() as connection:
                    workers = connection.execute(
                        "SELECT id, display_name FROM users WHERE role = 'worker' ORDER BY display_name, id"
                    ).fetchall()
                    reports = [
                        report
                        for worker in workers
                        if (report := write_attendance_report(connection, worker["id"]))
                    ]
            except OSError:
                self.send_json(500, {"error": "No se pudieron actualizar los informes de fichaje."})
                return
            self.send_json(
                200,
                {
                    "reports": reports,
                    "workers": [
                        {"id": worker["id"], "name": worker["display_name"]}
                        for worker in workers
                    ],
                    "shifts": [
                        shift
                        for report in reports
                        for shift in report["shifts"]
                    ],
                },
            )
            return
        if path == "/api/admin/monthly-finances":
            if not self.require_role("admin"):
                return
            report_directory = DATABASE_PATH.parent / "control mensual de gastos e ingresos"
            try:
                reports = []
                if report_directory.exists():
                    for report_path in sorted(report_directory.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].json")):
                        content = report_path.read_text(encoding="utf-8")
                        reports.append({
                            "filename": report_path.name,
                            "content": content,
                        })
            except OSError:
                self.send_json(500, {"error": "No se pudieron cargar los registros financieros."})
                return
            self.send_json(200, {"reports": reports})
            return
        if path == "/api/admin/security/login-attempts":
            if not self.require_role("admin"):
                return
            try:
                with database() as connection:
                    attempts = connection.execute(
                        """
                        SELECT attempted_at, ip_address, failure_type
                        FROM admin_login_attempts
                        ORDER BY id DESC
                        LIMIT 200
                        """
                    ).fetchall()
                    blocked_ips = connection.execute(
                        """
                        SELECT ip_address, blocked_at
                        FROM admin_blocked_ips
                        ORDER BY ip_address
                        """
                    ).fetchall()
            except OSError:
                self.send_json(500, {"error": "No se pudieron cargar los intentos de acceso."})
                return
            self.send_json(
                200,
                {
                    "attempts": [dict(row) for row in attempts],
                    "blockedIps": [dict(row) for row in blocked_ips],
                },
            )
            return
        if path == "/api/admin/active":
            if not self.require_role("admin"):
                return
            with database() as connection:
                rows = connection.execute(
                    """
                    SELECT u.id, u.display_name AS name,
                           COALESCE(e.department, u.department, 'Sin asignar') AS department,
                           e.recorded_at,
                           e.latitude, e.longitude, e.accuracy
                    FROM attendance_events e
                    JOIN users u ON u.id = e.user_id
                    WHERE e.event_type = 'start'
                      AND e.location_available = 1
                      AND substr(e.recorded_at, 1, 10) = ?
                      AND e.id = (
                          SELECT latest.id FROM attendance_events latest
                          WHERE latest.user_id = e.user_id
                          ORDER BY latest.recorded_at DESC, latest.id DESC LIMIT 1
                      )
                    ORDER BY e.recorded_at DESC
                    """,
                    (utc_now()[:10],),
                ).fetchall()
            self.send_json(200, {"workers": [dict(row) for row in rows]})
            return
        if path == "/api/admin/workers":
            if not self.require_role("admin"):
                return
            with database() as connection:
                rows = connection.execute(
                    """
                    SELECT id, first_name, last_name, department, pin
                    FROM users
                    WHERE role = 'worker' AND pin IS NOT NULL
                    ORDER BY CASE department
                        WHEN 'limpieza' THEN 1
                        WHEN 'conserjería' THEN 2
                        WHEN 'jardinería' THEN 3
                        ELSE 4 END, id
                    """
                ).fetchall()
            self.send_json(200, {"workers": [dict(row) for row in rows]})
            return
        if path == "/api/attendance/mine":
            user = self.require_role("worker")
            if not user:
                return
            with database() as connection:
                rows = connection.execute(
                    """
                    SELECT event_type, recorded_at,
                           CASE WHEN location_available = 1 THEN latitude ELSE NULL END AS latitude,
                           CASE WHEN location_available = 1 THEN longitude ELSE NULL END AS longitude,
                           CASE WHEN location_available = 1 THEN accuracy ELSE NULL END AS accuracy,
                           location_available,
                           worker_name, department
                    FROM attendance_events
                    WHERE user_id = ?
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT 20
                    """,
                    (user["id"],),
                ).fetchall()
            events = [dict(row) for row in rows]
            active = bool(events and events[0]["event_type"] == "start")
            self.send_json(200, {"active": active, "events": events})
            return

        user = self.current_user()
        if path in ("/", "/index.html", "/admin/dashboard.html"):
            selection_page = (
                path in ("/", "/index.html")
                and parse_qs(urlparse(self.path).query).get("select") == ["1"]
            )
            if user and user["role"] == "worker" and not selection_page:
                self.send_response(302)
                self.send_header("Location", "/worker/fichar.html")
                self.end_headers()
                return
            if path == "/admin/dashboard.html" and not user:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.path = "/index.html"
            return super().do_GET()
        if path == "/worker/fichar.html":
            if user and user["role"] == "admin":
                self.send_response(302)
                self.send_header("Location", "/admin/dashboard.html")
                self.end_headers()
                return
            if not user:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.path = "/control_horario_trabajador.html"
            return super().do_GET()
        self.send_error(404, "Ruta no encontrada.")

    def do_HEAD(self):
        self.send_error(405, "Método no permitido.")

    def do_PUT(self):
        path = urlparse(self.path).path
        attendance_match = re.fullmatch(r"/api/admin/attendance-reports/(\d+)", path)
        if attendance_match:
            if not self.require_role("admin"):
                return
            try:
                payload = self.read_json()
                with database() as connection:
                    shift, report = update_attendance_shift(
                        connection,
                        int(attendance_match.group(1)),
                        payload,
                    )
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except LookupError as error:
                self.send_json(404, {"error": str(error)})
                return
            except OSError:
                self.send_json(500, {"error": "No se pudo actualizar el archivo de fichaje."})
                return
            self.send_json(200, {"ok": True, "shift": shift, "report": report})
            return

        match = re.fullmatch(r"/api/admin/workers/(\d+)", path)
        if not match:
            self.send_json(404, {"error": "Ruta no encontrada."})
            return
        if not self.require_role("admin"):
            return
        try:
            payload = self.read_json()
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        first_name = payload.get("first_name")
        last_name = payload.get("last_name")
        if (
            not isinstance(first_name, str)
            or not isinstance(last_name, str)
            or not first_name.strip()
            or not last_name.strip()
            or len(first_name.strip()) > 60
            or len(last_name.strip()) > 60
        ):
            self.send_json(400, {"error": "Indica un nombre y apellido válidos (máximo 60 caracteres cada uno)."})
            return
        first_name = first_name.strip()
        last_name = last_name.strip()
        with database() as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET first_name = ?, last_name = ?, display_name = ?
                WHERE id = ? AND role = 'worker' AND pin IS NOT NULL
                """,
                (first_name, last_name, f"{first_name} {last_name}", int(match.group(1))),
            )
            if cursor.rowcount == 0:
                self.send_json(404, {"error": "No se encontró el operario solicitado."})
                return
            row = connection.execute(
                "SELECT id, first_name, last_name, display_name, department, pin FROM users WHERE id = ?",
                (int(match.group(1)),),
            ).fetchone()
        self.send_json(200, {"worker": dict(row)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            max_bytes = (
                (MAX_SIGNING_DOCUMENT_BYTES * 4 // 3) + 16_384
                if path == "/api/admin/signing/documents"
                else MAX_REQUEST_BYTES
            )
            payload = self.read_json(max_bytes=max_bytes)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        if path == "/api/login":
            username = payload.get("username")
            pin = payload.get("pin")
            if not isinstance(username, str) or not username:
                self.send_json(400, {"error": "Indica el nombre de usuario y un PIN de 4 dígitos."})
                return
            is_admin_login = username == "administrador"
            client_ip = self.client_address[0]
            if is_admin_login and is_admin_ip_blocked(client_ip):
                record_admin_login_event(client_ip, "Acceso rechazado: IP bloqueada")
                self.send_json(423, {"error": "El inicio de sesión de administrador está bloqueado desde esta dirección IP."})
                return
            if not is_admin_login and self.login_is_rate_limited():
                self.send_json(429, {"error": "Demasiados intentos. Espera 15 minutos antes de volver a intentarlo."})
                return
            if is_admin_login and admin_login_is_locked():
                record_admin_login_event(client_ip, "Acceso rechazado: cuenta de administrador bloqueada")
                self.send_json(
                    423,
                    {"error": "La cuenta del administrador está bloqueada tras 5 intentos fallidos. Reactívala desde el PC servidor."},
                )
                return
            if not isinstance(pin, str) or not re.fullmatch(r"\d{4}", pin):
                if is_admin_login:
                    locked = record_admin_login_failure(client_ip, "PIN con formato no válido")
                    self.send_json(
                        423 if locked else 401,
                        {
                            "error": (
                                "La cuenta del administrador está bloqueada tras 5 intentos fallidos. Reactívala desde el PC servidor."
                                if locked
                                else "Usuario o PIN incorrectos."
                            )
                        },
                    )
                    return
                self.send_json(400, {"error": "El PIN debe tener exactamente 4 dígitos."})
                return
            with database() as connection:
                if is_admin_login:
                    row = connection.execute(
                        """
                        SELECT id, username, display_name, role, first_name, last_name, department, pin
                        FROM users WHERE username = 'administrador' AND role = 'admin'
                        """
                    ).fetchone()
                    if not row or not hmac.compare_digest(pin, row["pin"]):
                        row = None
                else:
                    candidates = connection.execute(
                        """
                        SELECT id, username, display_name, role, first_name, last_name, department, pin
                        FROM users
                        WHERE role = 'worker' AND pin IS NOT NULL AND display_name = ?
                        """,
                        (username,),
                    ).fetchall()
                    row = next(
                        (candidate for candidate in candidates if hmac.compare_digest(pin, candidate["pin"])),
                        None,
                    )

            if not row:
                if is_admin_login:
                    locked = record_admin_login_failure(client_ip, "PIN incorrecto")
                    self.send_json(
                        423 if locked else 401,
                        {
                            "error": (
                                "La cuenta del administrador está bloqueada tras 5 intentos fallidos. Reactívala desde el PC servidor."
                                if locked
                                else "Usuario o PIN incorrectos."
                            )
                        },
                    )
                    return
                self.record_login_failure()
                self.send_json(401, {"error": "Usuario/departamento o PIN incorrectos."})
                return
            if is_admin_login:
                reset_admin_login_failures()
            else:
                self.clear_login_failures()

            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + SESSION_DURATION
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            old_session = cookie.get(COOKIE_NAME)
            old_token_hash = (
                hashlib.sha256(old_session.value.encode("utf-8")).hexdigest()
                if old_session
                else None
            )
            with database() as connection:
                if old_token_hash:
                    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (old_token_hash,))
                connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))
                connection.execute(
                    "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                    (
                        hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        row["id"],
                        expires_at.isoformat(timespec="milliseconds"),
                    ),
                )
            user = {
                key: row[key]
                for key in ("id", "username", "display_name", "role", "first_name", "last_name", "department")
            }
            self.send_json(
                200,
                {"user": user},
                {
                    "Set-Cookie": (
                        f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
                        f"Max-Age={int(SESSION_DURATION.total_seconds())}"
                        f"{'; Secure' if os.environ.get('ATTENDANCE_SECURE_COOKIE') == '1' else ''}"
                    )
                },
            )
            return

        if path == "/api/admin/signing/documents":
            user = self.require_role("admin")
            if not user:
                return
            filename = payload.get("filename")
            content_base64 = payload.get("content")
            if (
                not isinstance(filename, str)
                or not filename.strip()
                or len(filename) > 255
                or not isinstance(content_base64, str)
            ):
                self.send_json(400, {"error": "Selecciona un archivo PDF válido."})
                return
            filename = re.split(r"[\\/]", filename.strip())[-1]
            if not filename.lower().endswith(".pdf"):
                self.send_json(400, {"error": "Solo se pueden añadir documentos PDF."})
                return
            try:
                content = base64.b64decode(content_base64, validate=True)
            except (ValueError, base64.binascii.Error):
                self.send_json(400, {"error": "El contenido PDF enviado no es válido."})
                return
            if len(content) > MAX_SIGNING_DOCUMENT_BYTES:
                self.send_json(413, {"error": "El PDF supera el límite de 10 MB."})
                return
            if not content.startswith(b"%PDF-"):
                self.send_json(400, {"error": "El archivo seleccionado no parece ser un PDF válido."})
                return
            document_id = secrets.token_hex(16)
            storage_name = f"{document_id}.pdf"
            file_path = DATABASE_PATH.parent / "documentos_firma" / storage_name
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(content)
                with database() as connection:
                    connection.execute(
                        """
                        INSERT INTO signing_documents
                            (id, filename, storage_name, sha256, uploaded_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (document_id, filename, storage_name, hashlib.sha256(content).hexdigest(), utc_now()),
                    )
            except (OSError, sqlite3.Error):
                file_path.unlink(missing_ok=True)
                self.send_json(500, {"error": "No se pudo guardar el documento pendiente de firma."})
                return
            self.send_json(201, {"ok": True, "id": document_id, "filename": filename})
            return

        signed_document_match = re.fullmatch(
            r"/api/admin/signing/documents/([a-f0-9]{32})/signed",
            path,
        )
        if signed_document_match:
            user = self.require_role("admin")
            if not user:
                return
            signed_sha256 = payload.get("sha256")
            if not isinstance(signed_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", signed_sha256):
                self.send_json(400, {"error": "La huella SHA-256 del documento firmado no es válida."})
                return
            try:
                with database() as connection:
                    cursor = connection.execute(
                        """
                        UPDATE signing_documents
                        SET status = 'signed', signed_sha256 = ?, signed_at = ?, signed_by = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (signed_sha256, utc_now(), user["id"], signed_document_match.group(1)),
                    )
            except sqlite3.Error:
                self.send_json(500, {"error": "No se pudo actualizar el estado del documento."})
                return
            if cursor.rowcount == 0:
                self.send_json(404, {"error": "El documento no existe o ya está marcado como firmado."})
                return
            self.send_json(200, {"ok": True, "status": "signed", "signed_sha256": signed_sha256})
            return

        if path == "/api/logout":
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get(COOKIE_NAME)
            if morsel:
                token_hash = hashlib.sha256(morsel.value.encode("utf-8")).hexdigest()
                with database() as connection:
                    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            self.send_json(
                200,
                {"ok": True},
                {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
            )
            return

        if path == "/api/admin/monthly-finances":
            if not self.require_role("admin"):
                return
            try:
                record = normalize_monthly_finance(payload)
                report_directory = DATABASE_PATH.parent / "control mensual de gastos e ingresos"
                report_path = report_directory / f"{record['month']}.json"
                write_json_atomically(report_path, record)
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except OSError:
                self.send_json(500, {"error": "No se pudo guardar el registro financiero."})
                return
            self.send_json(201, {"ok": True, "record": record, "filename": report_path.name})
            return

        if path == "/api/admin/security/blocked-ips":
            user = self.require_role("admin")
            if not user:
                return
            action = payload.get("action")
            try:
                ip_address = validate_ip_address(payload.get("ip"))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            if action == "block":
                try:
                    with database() as connection:
                        connection.execute(
                            """
                            INSERT INTO admin_blocked_ips (ip_address, blocked_at, blocked_by)
                            VALUES (?, ?, ?)
                            ON CONFLICT(ip_address) DO NOTHING
                            """,
                            (ip_address, utc_now(), user["id"]),
                        )
                except OSError:
                    self.send_json(500, {"error": "No se pudo bloquear la dirección IP."})
                    return
                self.send_json(200, {"ok": True, "ip": ip_address, "blocked": True})
                return
            if action == "unblock":
                try:
                    with database() as connection:
                        connection.execute(
                            "DELETE FROM admin_blocked_ips WHERE ip_address = ?",
                            (ip_address,),
                        )
                except OSError:
                    self.send_json(500, {"error": "No se pudo desbloquear la dirección IP."})
                    return
                self.send_json(200, {"ok": True, "ip": ip_address, "blocked": False})
                return
            self.send_json(400, {"error": "La acción debe ser block o unblock."})
            return

        if path in ("/api/attendance/start", "/api/attendance/stop"):
            user = self.require_role("worker")
            if not user:
                return
            location_available = payload.get("locationAvailable") is not False
            if location_available:
                try:
                    latitude = float(payload["latitude"])
                    longitude = float(payload["longitude"])
                    accuracy = float(payload["accuracy"])
                    location_timestamp = payload["locationTimestamp"]
                    if (
                        not all(map(math.isfinite, (latitude, longitude, accuracy)))
                        or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
                        or not (0 <= accuracy <= 10_000)
                        or not isinstance(location_timestamp, str)
                        or len(location_timestamp) > 64
                    ):
                        raise ValueError
                    parsed_timestamp = datetime.fromisoformat(location_timestamp.replace("Z", "+00:00"))
                    if parsed_timestamp.tzinfo is None:
                        raise ValueError
                except (KeyError, TypeError, ValueError, OverflowError):
                    location_available = False
            if not location_available:
                latitude = 0
                longitude = 0
                accuracy = 10_000
                location_timestamp = ""

            event_type = "start" if path.endswith("/start") else "stop"
            try:
                with database() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    latest = connection.execute(
                        "SELECT event_type, recorded_at FROM attendance_events WHERE user_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
                        (user["id"],),
                    ).fetchone()
                    if event_type == "start" and latest and latest["event_type"] == "start":
                        self.send_json(409, {"error": "Ya hay una jornada activa."})
                        return
                    if event_type == "stop" and (not latest or latest["event_type"] != "start"):
                        self.send_json(409, {"error": "No hay una jornada activa que detener."})
                        return
                    recorded_at = utc_now()
                    connection.execute(
                        """
                        INSERT INTO attendance_events
                            (user_id, event_type, recorded_at, latitude, longitude, accuracy, location_timestamp,
                             worker_name, department, location_available)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user["id"], event_type, recorded_at, latitude, longitude, accuracy, location_timestamp,
                            user["display_name"], user["department"] or "Sin asignar", int(location_available),
                        ),
                    )
                    report = write_attendance_report(connection, user["id"])
            except OSError:
                self.send_json(500, {"error": "No se pudo guardar el informe de fichaje; el fichaje no se registró."})
                return
            result = {"event": event_type, "recordedAt": recorded_at}
            result["locationAvailable"] = location_available
            if event_type == "stop":
                start_time = datetime.fromisoformat(latest["recorded_at"].replace("Z", "+00:00"))
                result["shift"] = {
                    "worker": user["display_name"],
                    "date": start_time.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                    "startedAt": latest["recorded_at"],
                    "stoppedAt": recorded_at,
                }
                result["report"] = report["filename"]
            self.send_json(201, result)
            return

        self.send_json(404, {"error": "Ruta no encontrada."})

    def do_OPTIONS(self):
        self.send_json(405, {"error": "Método no permitido."})


def serve(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", "3000"))
    initialize_database()
    server = ThreadingHTTPServer((host, port), AttendanceHandler)
    print(f"Servidor disponible en http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Servidor de control horario con sesiones y SQLite.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create-user", help="Crear una cuenta con rol admin o worker.")
    create_parser.add_argument("--username", required=True)
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--role", required=True, choices=("admin", "worker"))
    subparsers.add_parser("unlock-admin", help="Desbloquear la cuenta administrador tras intentos fallidos.")
    serve_parser = subparsers.add_parser("serve", help="Iniciar el servidor web.")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")))
    args = parser.parse_args()

    if args.command == "create-user":
        import getpass

        password = getpass.getpass("Contraseña (mínimo 12 caracteres): ")
        confirmation = getpass.getpass("Confirma la contraseña: ")
        if not hmac.compare_digest(password, confirmation):
            parser.error("Las contraseñas no coinciden.")
        try:
            initialize_database()
            create_user(args.username, args.name, args.role, password)
        except (ValueError, sqlite3.IntegrityError) as error:
            parser.error(str(error))
        print(f"Cuenta {args.username} creada con rol {args.role}.")
    elif args.command == "unlock-admin":
        try:
            unlock_admin_account()
        except LookupError as error:
            parser.error(str(error))
        print("Cuenta administrador desbloqueada. Ya puede volver a iniciar sesión con su PIN.")
    else:
        if not 1 <= args.port <= 65535:
            parser.error("El puerto debe estar entre 1 y 65535.")
        serve(args.host, args.port)


if __name__ == "__main__":
    main()
