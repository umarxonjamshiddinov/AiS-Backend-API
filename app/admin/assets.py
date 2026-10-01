"""Serve one consistent HTML/JS/CSS snapshot with content-addressed asset URLs."""

import hashlib
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response


class AdminAssets:
    def __init__(self, html: str, files: dict[str, bytes]):
        self.files = {}
        urls = {}
        for name, content in files.items():
            digest = hashlib.sha256(content).hexdigest()[:20]
            self.files[(digest, name)] = content
            urls[name] = f"/admin/assets/{digest}/{name}"
        self.html = re.sub(
            r'/static/(admin\.(?:js|css))(?:\?[^"\s>]*)?',
            lambda match: urls[match[1]],
            html,
        )

    @classmethod
    def from_directory(cls, directory: Path):
        return cls(
            directory.joinpath("admin.html").read_text(encoding="utf-8"),
            {name: directory.joinpath(name).read_bytes() for name in ("admin.js", "admin.css")},
        )

    def response(self, digest: str, name: str, if_none_match: str | None):
        content = self.files.get((digest, name))
        if content is None:
            raise HTTPException(404, "Asset not found")
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{digest}"',
            "X-Content-Type-Options": "nosniff",
        }
        if if_none_match == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        media_type = "application/javascript" if name.endswith(".js") else "text/css"
        return Response(content, media_type=media_type, headers=headers)
