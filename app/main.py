import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.admin.assets import AdminAssets
from app.admin.routes import page_router
from app.admin.routes import router as admin_router
from app.api.routes import router
from app.core.config import PROJECT_ROOT, get_settings
from app.gateway.middleware import RequestSizeLimitMiddleware
from app.gateway.quota import reap_expired
from app.gateway.routes import router as gateway_router
from app.providers.base import ProviderError
from app.safety.routes import router as safety_router


async def maintain_reservations():
    while True:
        await asyncio.sleep(get_settings().reservation_reaper_seconds)
        try:
            await run_in_threadpool(reap_expired)
        except (SQLAlchemyError, ProviderError):
            logging.getLogger(__name__).warning("Reservation maintenance temporarily unavailable")


def create_app(*, maintenance=True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        task = asyncio.create_task(maintain_reservations()) if maintenance else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    application = FastAPI(title="AiS Backend API", version="0.5.0", lifespan=lifespan)
    application.state.admin_assets = AdminAssets.from_directory(PROJECT_ROOT / "app/static")
    application.add_middleware(RequestSizeLimitMiddleware)
    application.include_router(router)
    application.include_router(gateway_router)
    application.include_router(safety_router)
    application.include_router(admin_router)
    application.include_router(page_router)
    application.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app/static"), name="static")

    @application.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError):
        headers = {"Retry-After": str(exc.retry_after or 60)} if exc.status_code == 429 else {}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if getattr(exc, "request_id", None):
            headers["X-Request-ID"] = exc.request_id
        return JSONResponse(status_code=exc.status_code, headers=headers, content=exc.as_dict())

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return JSONResponse(
            status_code=503,
            content=ProviderError(
                "database_unavailable",
                503,
                message="The gateway database is temporarily unavailable.",
            ).as_dict(),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo credential fields, headers, prompts, or raw validation inputs.
        public = request.url.path.startswith("/v1")
        fields = [".".join(map(str, e["loc"][1:])) for e in exc.errors()]
        param = fields[0] if fields else None
        # Field names from extra_forbidden may be arbitrary user input; bound and sanitize them.
        if param and (len(param) > 100 or not all(c.isalnum() or c in "._-" for c in param)):
            param = None
        error = ProviderError(
            "invalid_request",
            400,
            param=param,
            message="Invalid request parameters; check the indicated field.",
        )
        content = error.as_dict()
        if not public:
            content["fields"] = [param] if param else []
        return JSONResponse(status_code=400 if public else 422, content=content)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/v1"):
            code = {404: "not_found", 405: "method_not_allowed", 413: "request_too_large"}.get(
                exc.status_code, "invalid_request"
            )
            return JSONResponse(
                status_code=exc.status_code, content=ProviderError(code, exc.status_code).as_dict()
            )
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
        )

    @application.middleware("http")
    async def security_headers(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger(__name__).error("Unhandled gateway request failure")
            response = JSONResponse(
                status_code=500,
                content=ProviderError(
                    "internal_error",
                    500,
                    message="The gateway could not complete this request.",
                ).as_dict(),
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith(("/admin", "/v1")):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.path.startswith("/static/"):
            # Legacy unversioned URLs must not become fresh stale cache entries.
            response.headers["Cache-Control"] = "no-store"
        return response

    return application


app = create_app()
