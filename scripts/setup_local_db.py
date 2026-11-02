"""Create a project-local PostgreSQL cluster. Never prints credentials."""
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
BIN = Path(
    os.getenv(
        'POSTGRES_BIN',
        os.getenv('AIS_POSTGRES_BIN', r'C:\Program Files\PostgreSQL\18\bin'),
    )
)
PORT = 55432


def command(args):
    result = subprocess.run(args, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('PostgreSQL command failed; inspect project-local postgres log')


def main():
    import importlib

    importlib.import_module("app.db")  # Initialize Windows libpq before driver import.
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url

    if not (BIN / 'initdb.exe').exists():
        raise RuntimeError('Set AIS_POSTGRES_BIN to installed PostgreSQL bin directory')
    RUNTIME.mkdir(exist_ok=True)
    data = RUNTIME / 'postgres'
    env_path = ROOT / '.env'
    values = dotenv_values(env_path)
    if values.get('DATABASE_URL'):
        url = make_url(values['DATABASE_URL'])
        if url.host != '127.0.0.1' or url.port != PORT or url.username != 'ais_owner':
            raise RuntimeError('Existing DATABASE_URL is external; local setup will not change it')
        password = url.password
    else:
        if (data / 'PG_VERSION').exists():
            raise RuntimeError('Cluster exists without DATABASE_URL; recover credentials first')
        password = secrets.token_hex(32)
    if not (data / 'PG_VERSION').exists():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', PORT))
        password_file = RUNTIME / '.init-password'
        password_file.write_text(password, encoding='utf-8')
        try:
            command([str(BIN / 'initdb.exe'), '-D', str(data), '-U', 'ais_owner',
                     '--auth=scram-sha-256', '--encoding=UTF8', '--locale=C',
                     '--pwfile=' + str(password_file)])
            with (data / 'postgresql.conf').open('a', encoding='utf-8') as config:
                config.write("\nlisten_addresses = '127.0.0.1'\nport = 55432\n")
        finally:
            password_file.unlink(missing_ok=True)
        set_key(str(env_path), 'DATABASE_URL',
                f'postgresql+psycopg://ais_owner:{password}@127.0.0.1:{PORT}/ais')
    if not values.get('ADMIN_TOKEN'):
        set_key(str(env_path), 'ADMIN_TOKEN', secrets.token_urlsafe(48))
    status = subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(data), 'status'],
                            capture_output=True)
    if status.returncode:
        import time
        with (RUNTIME / 'postgres.log').open('ab') as log:
            process = subprocess.Popen([str(BIN / 'postgres.exe'), '-D', str(data)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError('PostgreSQL process exited; inspect postgres.log')
            try:
                with socket.create_connection(('127.0.0.1', PORT), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('PostgreSQL startup timed out')
    with psycopg.connect(host='127.0.0.1', port=PORT, user='ais_owner',
                        password=password, dbname='postgres', autocommit=True) as db:
        for name in ('ais', 'ais_test'):
            if not db.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone():
                db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    set_key(str(env_path), 'TEST_DATABASE_URL',
            f'postgresql+psycopg://ais_owner:{password}@127.0.0.1:{PORT}/ais_test')
    print('Project PostgreSQL ready on 127.0.0.1:55432; credentials saved only in .env')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Local PostgreSQL setup failed; credentials were not printed')
        sys.exit(1)
