import time
import logging
import httpx
from typing import AsyncIterator, Dict, Any

from providers.base import LLMProvider
from core.schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage, ChatChoice, Usage, ProviderResult
from core.config import settings

logger = logging.getLogger("switchboard.groq")

_groq_client: httpx.AsyncClient | None = None


async def get_groq_client() -> httpx.AsyncClient:
    global _groq_client
    if _groq_client is None or _groq_client.is_closed:
        _groq_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _groq_client


class GroqProvider(LLMProvider):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.GROQ_API_KEY
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def generate(self, request: ChatCompletionRequest) -> ProviderResult:
        client = await get_groq_client()
        payload = request.model_dump(exclude_unset=True)

        start = time.time()
        response = await client.post(
            self.base_url,
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        latency_ms = (time.time() - start) * 1000
        data = response.json()

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

    async def generate_stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        client = await get_groq_client()
        payload = request.model_dump(exclude_unset=True)
        payload["stream"] = True

        async with client.stream(
            "POST",
            self.base_url,
            headers=self.headers,
            json=payload,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    async def list_models(self) -> list[dict]:
        try:
            url = "https://api.groq.com/openai/v1/models"
            client = await get_groq_client()
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
        except Exception as e:
            logger.warning(f"Failed to list models from Groq: {e}")
            return []

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        return True

    async def get_cost_per_token(self) -> Dict[str, float]:
        return {
            "input": 0.0000005,
            "output": 0.0000015
        }