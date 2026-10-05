#!/usr/bin/env python3
"""Add the public portal to an existing checkout, preserving local backend changes."""

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

MARKER = "# AiS public API portal integration"
ADDITION = (
    "\n\n" + MARKER + "\n"
    "from app.portal import mount_portal as _mount_ais_portal  # noqa: E402\n"
    "_mount_ais_portal(app)\n"
)
ROUTE_CHECK = r"""
import json
from app.main import app
def routes(items, prefix=''):
    for route in items:
        original = getattr(route, 'original_router', None)
        if original is not None:
            context = getattr(route, 'include_context', None)
            yield from routes(original.routes, prefix + getattr(context, 'prefix', ''))
        elif hasattr(route, 'path'):
            yield [prefix + route.path,
                sorted(getattr(route, 'methods', None) or []), type(route).__name__]
result = {'routes': sorted([
    route for route in routes(app.routes)
])}
if any(route[0] == '/' for route in result['routes']):
    from app.portal import configured_portal_directory
    directory = configured_portal_directory()
    result['portal_directory'] = str(directory) if directory else None
    import re
    import asyncio
    async def request(path, method='GET'):
        messages = []
        sent = False
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await asyncio.sleep(3600)
        async def send(message):
            messages.append(message)
        scope = {'type':'http', 'asgi':{'version':'3.0','spec_version':'2.4'},
            'http_version':'1.1', 'method':method, 'scheme':'http',
            'path':path, 'raw_path':path.encode(), 'query_string':b'',
            'root_path':'', 'headers':[(b'host',b'localhost')],
            'client':('127.0.0.1',12345), 'server':('127.0.0.1',8000)}
        await app(scope, receive, send)
        start = next(message for message in messages if message['type']=='http.response.start')
        headers = dict(start['headers'])
        body = b''.join(message.get('body',b'') for message in messages
            if message['type']=='http.response.body')
        return start['status'], headers, body
    async def verify():
        status, headers, body = await request('/')
        text = body.decode('utf-8')
        result['html_ok'] = (status == 200
            and headers.get(b'content-type',b'').startswith(b'text/html')
            and '<div id="root"' in text)
        assets = re.findall(r'(?:src|href)=[\"\'](/assets/[^\"\']+)', text)
        result['assets_ok'] = bool(assets)
        for path in assets:
            asset_status, _, _ = await request(path)
            result['assets_ok'] = result['assets_ok'] and asset_status == 200
        head_status, _, _ = await request('/', 'HEAD')
        result['head_ok'] = head_status == 200
    asyncio.run(verify())
print('AIS_PORTAL_CHECK=' + json.dumps(result))
"""


def atomic_write(path: Path, value: bytes, mode: int):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def check_routes(backend: Path):
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(backend)
    check = subprocess.run(
        [sys.executable, "-c", ROUTE_CHECK],
        cwd=backend,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if check.returncode:
        raise ValueError("Backend import check failed; no secrets or provider logs are printed.")
    output = next(
        (
            line.removeprefix("AIS_PORTAL_CHECK=")
            for line in check.stdout.splitlines()
            if line.startswith("AIS_PORTAL_CHECK=")
        ),
        None,
    )
    if output is None:
        raise ValueError("Backend import did not produce the expected route check.")
    return json.loads(output)


def deployment(backend: Path, package: Path):
    if not backend.is_absolute() or not package.is_absolute():
        raise ValueError("Use absolute --backend-root and --package-root paths.")
    backend, package = backend.resolve(), package.resolve()
    if not package.is_relative_to(backend / ".runtime"):
        raise ValueError("Stage the package inside this backend's .runtime directory.")
    main = backend / "app/main.py"
    source = package / "app/portal.py"
    installer_path = package / "scripts/install_portal.py"
    for path in (main, source, installer_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected an ordinary file: {path}")
    if os.environ.get("AIS_PORTAL_DIR"):
        raise ValueError(
            "AIS_PORTAL_DIR is set; resolve the existing override before installation."
        )

    original = main.read_bytes()
    original_mode = stat.S_IMODE(main.stat().st_mode)
    text = original.decode("utf-8-sig")
    tree = ast.parse(text)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "create_app"
    ]
    if len(assignments) != 1:
        raise ValueError("Expected exactly one top-level app = create_app() assignment.")
    if any(isinstance(node, ast.If) and "__name__" in ast.unparse(node.test) for node in tree.body):
        raise ValueError("Direct-launch code requires a separate integration review.")
    proposed = original if MARKER in text else (text.rstrip() + ADDITION).encode("utf-8")
    ast.parse(proposed.decode("utf-8"))
    module = backend / "app/portal.py"
    module_bytes = source.read_bytes()
    ast.parse(module_bytes.decode("utf-8"))
    if module.is_symlink() or (module.exists() and module.read_bytes() != module_bytes):
        raise ValueError("app/portal.py already has different content; it will not be overwritten.")

    before = check_routes(backend)
    reserved = {"/", "/assets", "/favicon.svg", "/robots.txt", "/sitemap.xml"}
    if MARKER not in text and any(
        route[0] in reserved or route[0] == "" or "{path:path}" in route[0]
        for route in before["routes"]
    ):
        raise ValueError("A public portal route already exists; refusing to mask it.")

    specification = importlib.util.spec_from_file_location("ais_portal_installer", installer_path)
    installer = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(installer)
    installer.validate_dist(package / "app/public_portal")
    runtime = backend / ".runtime"
    backup = runtime / (
        "portal-backup-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    backup.mkdir()
    shutil.copy2(main, backup / "main.py")
    active = runtime / "portal/active.json"
    old_manifest = active.read_bytes() if active.exists() else None
    if active.is_symlink():
        raise ValueError("The active manifest must not be a symbolic link.")
    if old_manifest is not None:
        (backup / "active.json").write_bytes(old_manifest)
    (backup / "state.json").write_text(
        json.dumps(
            {
                "backend": str(backend),
                "manifest_existed": old_manifest is not None,
                "main_sha256": hashlib.sha256(original).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    changed_main = False
    try:
        release, _ = installer.install(backend, package / "app/public_portal")
        if main.read_bytes() != original:
            raise ValueError("main.py changed during installation; stopped to preserve that edit.")
        if not module.exists():
            atomic_write(module, module_bytes, 0o644)
        if proposed != original:
            atomic_write(main, proposed, original_mode)
            changed_main = True
        after = check_routes(backend)
        if any(route not in after["routes"] for route in before["routes"]):
            raise ValueError("An existing backend route changed during portal integration.")
        additions = [route for route in after["routes"] if route not in before["routes"]]
        if any(route[0] not in reserved for route in additions):
            raise ValueError("Unexpected routes appeared during integration.")
        if not all(any(route[0] == path for route in after["routes"]) for path in ("/", "/assets")):
            raise ValueError("Portal routes were not registered.")
        if after.get("portal_directory") != str(release):
            raise ValueError("An existing configuration overrides the selected portal release.")
        if not all(after.get(key) for key in ("html_ok", "assets_ok", "head_ok")):
            raise ValueError("Portal HTML, HEAD, or assets did not pass the local HTTP check.")
    except Exception:
        if changed_main:
            atomic_write(main, original, original_mode)
        if old_manifest is not None:
            atomic_write(active, old_manifest, 0o644)
        elif active.exists():
            active.unlink()
        raise

    print("PORTAL_READY")
    print(f"Release: {release}")
    print(f"Backup: {backup}")
    print("Existing backend routes and local source edits were preserved.")
    print("Restart only ais-api.service, then verify the public website and /health.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        deployment(args.backend_root, args.package_root)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Portal deployment stopped: {error}", file=sys.stderr)
        print(
            "The API service was not restarted. Existing main.py and manifest were preserved.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
