param([int]$Port = 8000, [switch]$SkipLocalDatabaseSetup)
$ErrorActionPreference = 'Stop'
$aisProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $aisProjectRoot
$aisPython = Join-Path $aisProjectRoot '.venv\Scripts\python.exe'
if (-not $SkipLocalDatabaseSetup) {
    & $aisPython scripts/setup_local_db.py
    if ($LASTEXITCODE -ne 0) { throw 'Local PostgreSQL setup failed' }
}
& $aisPython -m app.manage init-db
if ($LASTEXITCODE -ne 0) { throw 'Database migration or catalog seed failed' }
& $aisPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
