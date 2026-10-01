from app.providers.base import InferenceProvider
from app.schemas.inference import InferenceRequest, InferenceResponse


class InferenceService:
    def __init__(self, provider: InferenceProvider, *, model: str):
        self.provider = provider
        self.model = model

    def generate(self, request: InferenceRequest) -> InferenceResponse:
        return self.provider.generate(request, model=self.model)
