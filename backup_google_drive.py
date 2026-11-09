import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(
    os.environ.get(
        "ATTENDANCE_DATABASE",
        ROOT / "Almacenamiento" / "control_horario.sqlite3",
    )
)
BACKUP_PREFIX = "grupo-ada-backup-"
BACKUP_FOLDER_NAME = "Grupo ADA Backups"
BACKUP_DIRECTORIES = (
    "archivo_documental",
    "documentos_firma",
    "solicitudes_empleo",
    "control de fichaje",
    "control mensual de gastos e ingresos",
)


def create_backup_archive(destination, database_path=DATABASE_PATH, database_url=None):
    destination = Path(destination)
    database_path = Path(database_path)
    if not database_url and not database_path.is_file():
        raise FileNotFoundError(f"No se encontró la base de datos SQLite: {database_path}")

    with tempfile.TemporaryDirectory(prefix="grupo-ada-backup-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        if database_url:
            database_dump = temporary_root / "database.dump"
            create_postgres_dump(database_url, database_dump)
            database_archive_path = "database/postgresql.dump"
        else:
            database_dump = temporary_root / "control_horario.sqlite3"
            create_sqlite_snapshot(database_path, database_dump)
            database_archive_path = "database/control_horario.sqlite3"

        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database_dump, database_archive_path)
            storage_root = database_path.parent
            for directory_name in BACKUP_DIRECTORIES:
                source_directory = storage_root / directory_name
                if not source_directory.is_dir():
                    continue
                for source_file in source_directory.rglob("*"):
                    if not source_file.is_file() or source_file.is_symlink():
                        continue
                    archive.write(
                        source_file,
                        (Path("storage") / directory_name / source_file.relative_to(source_directory)).as_posix(),
                    )


def create_sqlite_snapshot(source, destination):
    import sqlite3

    with closing(sqlite3.connect(source)) as source_connection, closing(
        sqlite3.connect(destination)
    ) as backup_connection:
        source_connection.backup(backup_connection)


def create_postgres_dump(database_url, destination):
    parsed_url = urlparse(database_url)
    if parsed_url.scheme not in ("postgres", "postgresql") or not parsed_url.hostname:
        raise ValueError("DATABASE_URL no contiene una URL válida de PostgreSQL.")

    command = shutil.which("pg_dump")
    if not command:
        raise RuntimeError(
            "No se encontró pg_dump. Ejecuta el respaldo en un entorno que tenga "
            "instalado el cliente PostgreSQL."
        )

    query = parse_qs(parsed_url.query)
    environment = {
        "PGHOST": parsed_url.hostname,
        "PGDATABASE": unquote(parsed_url.path.lstrip("/")),
        "PGSSLMODE": query.get("sslmode", ["require"])[0],
        "PGCONNECT_TIMEOUT": "10",
    }
    if parsed_url.port:
        environment["PGPORT"] = str(parsed_url.port)
    if parsed_url.username:
        environment["PGUSER"] = unquote(parsed_url.username)
    if parsed_url.password:
        environment["PGPASSWORD"] = unquote(parsed_url.password)
    if "sslrootcert" in query:
        environment["PGSSLROOTCERT"] = query["sslrootcert"][0]

    pg_environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        **environment,
    }
    result = subprocess.run(
        [command, "--format=custom", "--no-owner", "--no-acl", "--file", str(destination)],
        env=pg_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or "pg_dump terminó con error."
        raise RuntimeError(f"No se pudo respaldar PostgreSQL: {detail}")


def upload_backup_to_drive(archive_path, backup_name, folder_id=None, keep_backups=30):
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials

    client_id = os.environ.get("GOOGLE_DRIVE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN")
    if not all((client_id, client_secret, refresh_token)):
        raise ValueError(
            "Configura GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET y "
            "GOOGLE_DRIVE_REFRESH_TOKEN en Render."
        )

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    try:
        if not folder_id:
            folders = drive.files().list(
                q=(
                    "mimeType = 'application/vnd.google-apps.folder' "
                    f"and name = '{BACKUP_FOLDER_NAME}' and trashed = false"
                ),
                spaces="drive",
                fields="files(id,name)",
                pageSize=10,
            ).execute().get("files", [])
            if folders:
                folder_id = folders[0]["id"]
            else:
                folder = drive.files().create(
                    body={
                        "name": BACKUP_FOLDER_NAME,
                        "mimeType": "application/vnd.google-apps.folder",
                    },
                    fields="id",
                ).execute()
                folder_id = folder["id"]

        media = MediaFileUpload(
            str(archive_path),
            mimetype="application/zip",
            resumable=True,
            chunksize=8 * 1024 * 1024,
        )
        uploaded = drive.files().create(
            body={"name": backup_name, "parents": [folder_id]},
            media_body=media,
            fields="id,name,size,createdTime",
        ).execute()

        backups = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="files(id,name,createdTime)",
            pageSize=1000,
            orderBy="createdTime desc",
        ).execute().get("files", [])
        managed_backups = [
            item for item in backups
            if item.get("name", "").startswith(BACKUP_PREFIX)
        ]
        for stale_backup in managed_backups[keep_backups:]:
            drive.files().delete(fileId=stale_backup["id"]).execute()
        return uploaded
    except HttpError as error:
        raise RuntimeError(
            f"Google Drive rechazó el respaldo (HTTP {error.resp.status}). "
            "Comprueba las credenciales, el acceso a la carpeta y la cuota."
        ) from error


def run_backup(
    database_path=DATABASE_PATH,
    database_url=None,
    folder_id=None,
    keep_backups=None,
):
    database_path = Path(database_path)
    if database_url is None:
        database_url = os.environ.get("DATABASE_URL")
    if folder_id is None:
        folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if keep_backups is None:
        try:
            keep_backups = int(os.environ.get("GOOGLE_DRIVE_BACKUP_KEEP", "30"))
        except ValueError:
            raise ValueError("GOOGLE_DRIVE_BACKUP_KEEP debe ser un número entero.") from None
    if not 1 <= keep_backups <= 1000:
        raise ValueError("GOOGLE_DRIVE_BACKUP_KEEP debe estar entre 1 y 1000.")

    client_id = os.environ.get("GOOGLE_DRIVE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN")
    if not all((client_id, client_secret, refresh_token)):
        raise ValueError(
            "Configura GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET y "
            "GOOGLE_DRIVE_REFRESH_TOKEN en Render."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{BACKUP_PREFIX}{timestamp}.zip"
    with tempfile.TemporaryDirectory(prefix="grupo-ada-drive-upload-") as temporary_directory:
        archive_path = Path(temporary_directory) / backup_name
        create_backup_archive(
            archive_path,
            database_path=database_path,
            database_url=database_url,
        )
        return upload_backup_to_drive(
            archive_path,
            backup_name,
            folder_id=folder_id,
            keep_backups=keep_backups,
        )


def main():
    parser = argparse.ArgumentParser(description="Respalda la aplicación en Google Drive.")
    parser.parse_args()
    try:
        uploaded = run_backup()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error al crear el respaldo: {error}", file=sys.stderr)
        return 1
    print(f"Respaldo subido a Google Drive: {uploaded['name']} (id: {uploaded['id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
