from functools import lru_cache

from app.core.config import get_settings
from app.providers.bedrock import BedrockProvider
from app.services.inference import InferenceService


@lru_cache
def get_inference_service() -> InferenceService:
    settings = get_settings()
    return InferenceService(BedrockProvider(settings), model=settings.bedrock_model_id)
