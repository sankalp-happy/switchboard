import time
import logging
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("switchboard.openai_compatible")

from providers.base import LLMProvider
from core.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatChoice,
    Usage,
    ProviderResult,
)

_shared_client: httpx.AsyncClient | None = None


async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _shared_client


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str, provider_name: str = "openai-compatible"):
        if not base_url:
            raise ValueError("base_url is required for openai-compatible provider")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.provider_name = provider_name
        self.chat_completions_url = self._build_chat_completions_url(self.base_url)
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_chat_completions_url(base_url: str) -> str:
        cleaned = base_url.rstrip("/")
        if cleaned.endswith("/chat/completions"):
            return cleaned

        parsed = urlparse(cleaned)
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            return f"{cleaned}/chat/completions"

        return f"{cleaned}/v1/chat/completions"

    async def generate(self, request: ChatCompletionRequest) -> ProviderResult:
        client = await get_shared_client()
        payload = request.model_dump(exclude_unset=True)

        start = time.time()
        response = await client.post(
            self.chat_completions_url,
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        latency_ms = (time.time() - start) * 1000
        data = response.json()

        choices = []
        for item in data.get("choices", []):
            msg_data = item.get("message", {})
            message = ChatMessage(
                role=msg_data.get("role", "assistant"),
                content=msg_data.get("content", ""),
            )
            choice = ChatChoice(
                index=item.get("index", 0),
                message=message,
                finish_reason=item.get("finish_reason"),
            )
            choices.append(choice)

        usage_data = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        chat_response = ChatCompletionResponse(
            id=data.get("id", f"chatcmpl-{int(time.time())}"),
            created=data.get("created", int(time.time())),
            model=data.get("model", request.model),
            choices=choices,
            usage=usage,
        )

        rl_headers = {
            k.lower(): v
            for k, v in response.headers.items()
            if "ratelimit" in k.lower()
        }

        return ProviderResult(
            response=chat_response,
            provider=self.provider_name,
            latency_ms=latency_ms,
            rate_limit_headers=rl_headers,
        )

    async def generate_stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        client = await get_shared_client()
        payload = request.model_dump(exclude_unset=True)
        payload["stream"] = True

        async with client.stream(
            "POST",
            self.chat_completions_url,
            headers=self.headers,
            json=payload,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    async def list_models(self) -> list[dict]:
        try:
            models_url = self.base_url.rstrip("/")
            if models_url.endswith("/chat/completions"):
                models_url = models_url.replace("/chat/completions", "/models")
            elif models_url.endswith("/v1"):
                models_url = f"{models_url}/models"
            else:
                models_url = f"{models_url}/v1/models"
            client = await get_shared_client()
            response = await client.get(models_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
        except Exception as e:
            logger.warning(f"Failed to list models from {self.base_url}: {e}")
            return []

    async def health_check(self) -> bool:
        return bool(self.api_key and self.base_url)

    async def get_cost_per_token(self) -> dict[str, float]:
        return {
            "input": 0.0,
            "output": 0.0,
        }