from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.providers.base import ProviderError


class RequestSizeLimitMiddleware:
    """Bound raw multimodal JSON before Pydantic allocates decoded message objects."""

    def __init__(self, app, max_bytes=8 * 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            size = self.max_bytes + 1
        if size > self.max_bytes:
            error = ProviderError("request_too_large", 413, message="Request exceeds 8 MiB.")
            return await JSONResponse(error.as_dict(), status_code=413)(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(413, "request_too_large")
            return message

        await self.app(scope, bounded_receive, send)
