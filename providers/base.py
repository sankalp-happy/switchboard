from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, List, Any
from core.schemas import ChatCompletionRequest, ProviderResult


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, request: ChatCompletionRequest) -> ProviderResult:
        """Send a request and return ProviderResult (response + metadata/headers)."""
        pass

    async def generate_stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        """Stream SSE chunks from the provider. Yields raw bytes."""
        raise NotImplementedError("Streaming not supported by this provider")

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @abstractmethod
    async def get_cost_per_token(self) -> Dict[str, float]:
        """Returns input and output cost per token"""
        pass

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models from the provider's API. Returns list of model dicts with at least 'id' key."""
        return []
