from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text

from app.admin.routes import require_admin
from app.api.dependencies import get_inference_service
from app.db.models import ModelRoute
from app.db.session import session_factory
from app.providers.base import ProviderError
from app.schemas.inference import InferenceRequest, InferenceResponse
from app.services.inference import InferenceService

router = APIRouter()


@router.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", tags=["health"])
def ready():
    with session_factory()() as db:
        revision = db.scalar(text("SELECT version_num FROM alembic_version"))
        count = db.scalar(
            select(func.count())
            .select_from(ModelRoute)
            .where(ModelRoute.catalog_metadata["catalog"].astext == "cloud_bot_v7")
        )
        if revision != "0005" or count != 42:
            raise ProviderError(
                "database_not_initialized",
                503,
                message="Apply migrations and initialize the model catalog.",
            )
        return {"status": "ok", "database": "ok", "catalog_models": count, "version": "0.5.0"}


@router.post(
    "/internal/test/bedrock",
    response_model=InferenceResponse,
    tags=["internal"],
    dependencies=[Depends(require_admin)],
)
def test_bedrock(
    request: InferenceRequest,
    service: Annotated[InferenceService, Depends(get_inference_service)],
) -> InferenceResponse:
    try:
        return service.generate(request)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from None
