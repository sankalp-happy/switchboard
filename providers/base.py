from abc import ABC, abstractmethod
from typing import Dict, Any
from core.schemas import ChatCompletionRequest, ChatCompletionResponse

class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @abstractmethod
    async def get_cost_per_token(self) -> Dict[str, float]:
        """Returns input and output cost per token"""
        pass
