"""Windows libpq compatibility; no global PATH or installation changes."""
import os
from pathlib import Path

_libpq_handle = None
if os.name == "nt":
    pg_bin = Path(os.getenv("AIS_POSTGRES_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
    if (pg_bin / "libpq.dll").exists():
        os.environ.setdefault("PSYCOPG_IMPL", "python")
        os.environ["PATH"] = str(pg_bin) + os.pathsep + os.environ["PATH"]
        _libpq_handle = os.add_dll_directory(str(pg_bin))
