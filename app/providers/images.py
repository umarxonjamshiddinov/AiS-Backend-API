"""Validate image sources without fetching arbitrary URLs from the gateway host."""

import base64
import binascii
import re

from app.providers.base import ProviderError

DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)$")


def decode_image(url: str, *, max_bytes=3932160) -> tuple[str, bytes]:
    match = DATA_IMAGE.fullmatch(url)
    if not match:
        raise ProviderError(
            "invalid_request",
            400,
            param="messages",
            message="Use a base64 PNG, JPEG, GIF or WebP data URL.",
            usage_unknown=False,
        )
    try:
        data = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        raise ProviderError(
            "invalid_request",
            400,
            param="messages",
            message="Image base64 is invalid.",
            usage_unknown=False,
        ) from None
    signatures = {
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "gif": data.startswith((b"GIF87a", b"GIF89a")),
        "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if len(data) > max_bytes or not signatures[match[1]]:
        raise ProviderError(
            "invalid_request",
            400,
            param="messages",
            message="Image format or size is invalid (maximum 3.75 MiB).",
            usage_unknown=False,
        )
    return match[1], data
