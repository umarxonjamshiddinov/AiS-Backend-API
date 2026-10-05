"""Serve the optional public API portal without intercepting backend routes."""

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import PROJECT_ROOT


def configured_portal_directory() -> Path | None:
    """Select a release once at startup; an absent portal leaves the API unchanged."""
    override = os.environ.get("AIS_PORTAL_DIR")
    if override:
        directory = Path(override)
        return directory if directory.is_absolute() else None

    manifest = PROJECT_ROOT / ".runtime/portal/active.json"
    if not manifest.is_file():
        return None
    try:
        value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        directory_value = value.get("directory") if isinstance(value, dict) else None
        if not isinstance(directory_value, str):
            return None
        directory = Path(directory_value)
        return directory if directory.is_absolute() else None
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Public portal manifest could not be read")
        return None


class PortalAssets(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def mount_portal(application: FastAPI, directory: Path | None = None) -> bool:
    directory = directory if directory is not None else configured_portal_directory()
    if directory is None:
        return False
    directory = directory.resolve()
    index = directory / "index.html"
    assets = directory / "assets"
    if not index.is_file() or not assets.is_dir():
        logging.getLogger(__name__).warning(
            "Public portal release is incomplete; API kept available"
        )
        return False

    def homepage():
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    application.add_api_route("/", homepage, methods=["GET", "HEAD"], include_in_schema=False)
    application.mount("/assets", PortalAssets(directory=assets), name="portal-assets")

    def public_file(path: Path, media_type: str):
        def serve():
            return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})

        return serve

    for name, media_type in (
        ("favicon.svg", "image/svg+xml"),
        ("robots.txt", "text/plain"),
        ("sitemap.xml", "application/xml"),
    ):
        path = directory / name
        if path.is_file():
            application.add_api_route(
                f"/{name}",
                public_file(path, media_type),
                methods=["GET", "HEAD"],
                include_in_schema=False,
            )
    return True
