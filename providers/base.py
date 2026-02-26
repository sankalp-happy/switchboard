from abc import ABC, abstractmethod
from typing import Dict, Any
from core.schemas import ChatCompletionRequest, ProviderResult


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, request: ChatCompletionRequest) -> ProviderResult:
        """Send a request and return ProviderResult (response + metadata/headers)."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @abstractmethod
    async def get_cost_per_token(self) -> Dict[str, float]:
        """Returns input and output cost per token"""
        pass
