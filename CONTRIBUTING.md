# Contributing

## Development setup

Use Python 3.13 or newer. From the repository root, create a virtual environment and install the development dependencies:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Configure local database settings in `.env` as needed. Never commit `.env`, real credentials, production data, logs, database dumps, or backups. The `.env.example` file is the template for configuration.

## Checks

Run the relevant checks from the repository root before submitting changes:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pip check
node --check app\static\admin.js
```

Tests that need PostgreSQL require a suitable local test database configuration. Do not use production credentials or data for development or tests. The README documents additional database and smoke-test commands; avoid live or paid inference checks unless you are explicitly authorized and have configured a safe test environment.

## Changes and pull requests

- Keep changes focused and preserve the existing API and project conventions.
- Add or update tests and documentation when behavior or setup instructions change.
- Explain the purpose and scope of the change and list the checks you ran.
- Never include secrets, private deployment details, or sensitive screenshots in issues or pull requests. Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
