from typing import Optional, Dict

from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from providers.base import LLMProvider
from providers.groq_provider import GroqProvider

class Router:
    def __init__(self):
        # MVP: Only one provider (Groq). Later, this will hold a list of providers
        # and manage health states (Circuit Breaker).
        self.primary_provider = GroqProvider()
        
    async def route_request(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """
        Determine which provider to use. For MVP, we statically route to Groq.
        In the future, this will check:
        1. Context window limits
        2. Cost constraints
        3. Provider health (Circuit Breaker)
        """
        # Basic Circuit Breaker logic placeholder
        is_healthy = await self.primary_provider.health_check()
        if not is_healthy:
             raise Exception("Primary provider is currently unhealthy.")
             
        # Call provider
        return await self.primary_provider.generate(request)
