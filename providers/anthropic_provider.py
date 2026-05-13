import time
import httpx
from typing import Dict

from providers.base import LLMProvider
from core.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatChoice,
    Usage,
    ProviderResult,
)
from core.config import settings


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or getattr(settings, "ANTHROPIC_API_KEY", "")
        self.base_url = "https://api.anthropic.com/v1/messages"
        self.headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    async def generate(self, request: ChatCompletionRequest) -> ProviderResult:
        async with httpx.AsyncClient() as client:
            payload = self._build_payload(request)

            start = time.time()
            response = await client.post(
                self.base_url,
                headers=self.headers,
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            latency_ms = (time.time() - start) * 1000
            data = response.json()

            message_content = ""
            content_blocks = data.get("content", [])
            if content_blocks:
                first_block = content_blocks[0]
                message_content = first_block.get("text", "")

            choices = [
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=message_content),
                    finish_reason=data.get("stop_reason"),
                )
            ]

            usage_data = data.get("usage", {})
            usage = Usage(
                prompt_tokens=usage_data.get("input_tokens", 0),
                completion_tokens=usage_data.get("output_tokens", 0),
                total_tokens=usage_data.get("input_tokens", 0)
                + usage_data.get("output_tokens", 0),
            )

            chat_response = ChatCompletionResponse(
                id=data.get("id", f"chatcmpl-{int(time.time())}"),
                created=int(time.time()),
                model=data.get("model", request.model),
                choices=choices,
                usage=usage,
            )

            rl_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower().startswith("x-ratelimit")
            }

            return ProviderResult(
                response=chat_response,
                provider="anthropic",
                latency_ms=latency_ms,
                rate_limit_headers=rl_headers,
            )

    def _build_payload(self, request: ChatCompletionRequest) -> dict:
        messages = []
        system_text = None
        for msg in request.messages:
            if msg.role == "system" and system_text is None:
                system_text = msg.content
                continue
            messages.append({"role": msg.role, "content": msg.content})

        payload = {
            "model": request.model,
            "messages": messages,
            "max_tokens": 1024,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if system_text:
            payload["system"] = system_text
        return payload

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        return True

    async def get_cost_per_token(self) -> Dict[str, float]:
        return {
            "input": 0.0,
            "output": 0.0,
        }
