import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


POLL_INTERVAL_SECONDS = 15
MAX_WAIT_SECONDS = 6 * 60 * 60


def request_backup(url, token, method="POST", payload=None, timeout=30):
    body = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"El servidor rechazó la copia (HTTP {error.code}): {message}") from error
    except URLError as error:
        raise RuntimeError(f"No se pudo conectar con el servidor de Render: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("El servidor de Render devolvió una respuesta no válida.") from error


def run_trigger():
    url = os.environ.get("BACKUP_TRIGGER_URL", "").strip()
    token = os.environ.get("GOOGLE_DRIVE_BACKUP_TOKEN", "")
    if not url or not token:
        raise ValueError("Configura BACKUP_TRIGGER_URL y GOOGLE_DRIVE_BACKUP_TOKEN.")
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("BACKUP_TRIGGER_URL debe ser una URL HTTPS válida.")

    started = request_backup(url, token, timeout=300)
    backup_id = started.get("backupId")
    if not backup_id or started.get("status") != "running":
        raise RuntimeError("Render no confirmó el inicio del respaldo.")

    status_url = f"{url.rstrip('/')}/{backup_id}"
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        result = request_backup(status_url, token, method="GET")
        if result.get("status") == "succeeded":
            print(f"Respaldo de Google Drive completado: {result.get('name', backup_id)}")
            return
        if result.get("status") == "failed":
            raise RuntimeError(f"Falló el respaldo en Render: {result.get('error', 'error desconocido')}")
        if result.get("backupId") != backup_id or result.get("status") != "running":
            raise RuntimeError("El servidor de Render devolvió un estado de respaldo inesperado.")
    raise TimeoutError("El respaldo no terminó dentro del tiempo máximo de espera.")


def main():
    try:
        run_trigger()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"Error al solicitar el respaldo: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
