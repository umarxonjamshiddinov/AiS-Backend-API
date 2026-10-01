"""Create a project-local PostgreSQL backup without printing connection credentials."""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import PROJECT_ROOT, get_settings


def main():
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("Database not configured")
    url = make_url(settings.database_url.get_secret_value())
    destination = PROJECT_ROOT / ".runtime/backups"
    destination.mkdir(parents=True, exist_ok=True)
    filename = destination / (
        "ais-before-mantle-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".dump"
    )
    binary = (
        Path(os.getenv("AIS_POSTGRES_BIN", r"C:\Program Files\PostgreSQL\18\bin")) / "pg_dump.exe"
    )
    command = str(binary) if os.name == "nt" else "pg_dump"
    environment = {
        **os.environ,
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        "PGCONNECT_TIMEOUT": "5",
    }
    result = subprocess.run(
        [command, "--format=custom", "--no-password", "--file", str(filename)],
        env=environment,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode or not filename.exists() or filename.stat().st_size == 0:
        raise RuntimeError("Backup failed")
    print(str(filename))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("Database backup failed; no credentials were printed.", file=sys.stderr)
        sys.exit(1)
