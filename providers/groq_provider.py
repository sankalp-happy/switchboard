import time
import httpx
from typing import Dict, Any

from providers.base import LLMProvider
from core.schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage, ChatChoice, Usage, ProviderResult
from core.config import settings

class GroqProvider(LLMProvider):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.GROQ_API_KEY
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def generate(self, request: ChatCompletionRequest) -> ProviderResult:
        async with httpx.AsyncClient() as client:
            payload = request.model_dump(exclude_unset=True)
            
            # For MVP, disable streaming as it requires SSE handling
            payload['stream'] = False
            
            start = time.time()
            response = await client.post(
                self.base_url,
                headers=self.headers,
                json=payload,
                timeout=30.0
            )
            response.raise_for_status()
            latency_ms = (time.time() - start) * 1000
            data = response.json()
            
            # Map Groq response to unified schema
            choices = []
            for item in data.get("choices", []):
                msg_data = item.get("message", {})
                message = ChatMessage(role=msg_data.get("role", "assistant"), content=msg_data.get("content", ""))
                choice = ChatChoice(
                    index=item.get("index", 0),
                    message=message,
                    finish_reason=item.get("finish_reason")
                )
                choices.append(choice)
                
            usage_data = data.get("usage", {})
            usage = Usage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0)
            )
            
            chat_response = ChatCompletionResponse(
                id=data.get("id", f"chatcmpl-{int(time.time())}"),
                created=data.get("created", int(time.time())),
                model=data.get("model", request.model),
                choices=choices,
                usage=usage
            )

            # Extract rate-limit headers for key rotation tracking
            rl_headers = {
                k: v for k, v in response.headers.items()
                if k.lower().startswith("x-ratelimit")
            }

            return ProviderResult(
                response=chat_response,
                provider="groq",
                latency_ms=latency_ms,
                rate_limit_headers=rl_headers,
            )

    async def health_check(self) -> bool:
        # Simple health check, maybe hitting a very lightweight endpoint or just checking API key config
        if not self.api_key:
            return False
        # For simplicity in MVP, assume healthy if API key exists
        return True

    async def get_cost_per_token(self) -> Dict[str, float]:
        # Very rough estimates for Llama 3 on Groq
        return {
            "input": 0.0000005,
            "output": 0.0000015
        }
