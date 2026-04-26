"""
Router — simple key-availability-based routing.
No cost routing, no strategy matrix. Just:
  1. Pick the best provider key for the requested model
  2. Call the provider
  3. Update rate limits from response headers
  4. On 429 / exhaustion -> switch to next key and retry
"""

import logging
import httpx
from typing import AsyncIterator, Set

from core.schemas import ChatCompletionRequest, ChatCompletionResponse, ProviderResult
from core.key_manager import key_manager
from core.database import record_usage
from core.metrics import (
    PROVIDER_REQUESTS,
    PROVIDER_LATENCY,
    KEY_SWITCHES,
    TOKENS_PROCESSED,
)
from providers.groq_provider import GroqProvider
from providers.openai_compatible_provider import OpenAICompatibleProvider

logger = logging.getLogger("switchboard.router")


class Router:
    def __init__(self):
        self.supported_providers = {"groq", "openai-compatible"}

    async def route_request(self, request: ChatCompletionRequest) -> ProviderResult:
        """
        Try available keys in order of remaining quota.
        Retry with the next key on 429 or rate-limit exhaustion.
        """
        tried_key_ids: Set[int] = set()
        last_error = None
        last_provider = None

        # Try up to 10 keys (practical upper bound)
        for attempt in range(20):
            try:
                api_key, key_id, provider_name, base_url, _ = await key_manager.get_available_key_for_model(
                    model=request.model,
                    exclude_key_ids=tried_key_ids,
                    supported_providers=self.supported_providers,
                )
            except RuntimeError as e:
                last_error = e
                break

            if key_id in tried_key_ids:
                # We've cycled through all available keys
                break
            tried_key_ids.add(key_id)
            last_provider = provider_name

            if attempt > 0:
                KEY_SWITCHES.inc()
                logger.info(
                    "Switched to key id=%s provider=%s (attempt %s)",
                    key_id,
                    provider_name,
                    attempt + 1,
                )

            provider = self._build_provider(
                provider_name=provider_name,
                api_key=api_key,
                base_url=base_url,
            )

            try:
                result = await provider.generate(request)
                result.key_id = key_id

                # Update rate limits from response headers
                await key_manager.update_rate_limits(key_id, result.rate_limit_headers)

                # Record metrics
                PROVIDER_REQUESTS.labels(
                    provider=provider_name, key_label=str(key_id), status="success"
                ).inc()
                PROVIDER_LATENCY.labels(provider=provider_name).observe(
                    result.latency_ms / 1000
                )
                TOKENS_PROCESSED.labels(direction="input").inc(
                    result.response.usage.prompt_tokens
                )
                TOKENS_PROCESSED.labels(direction="output").inc(
                    result.response.usage.completion_tokens
                )

                # Record per-key usage for dashboard stats
                total_tokens = (
                    result.response.usage.prompt_tokens
                    + result.response.usage.completion_tokens
                )
                try:
                    await record_usage(key_id, total_tokens)
                except Exception as e:
                    logger.warning(f"Failed to record usage for key {key_id}: {e}")

                return result

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                last_error = e

                if status == 429:
                    logger.warning(f"Key id={key_id} got 429 — marking exhausted and retrying")
                    await key_manager.mark_key_exhausted(key_id)
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="rate_limited"
                    ).inc()
                    continue
                elif status == 401:
                    logger.warning(f"Key id={key_id} got 401 — invalid key, disabling and trying next")
                    await key_manager.toggle_key(key_id, enabled=False)
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="auth_error"
                    ).inc()
                    continue
                elif status >= 500:
                    logger.warning(f"Key id={key_id} got {status} — trying next key")
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="server_error"
                    ).inc()
                    continue
                else:
                    # 4xx other than 429/401 — don't retry
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="client_error"
                    ).inc()
                    raise

            except Exception as e:
                last_error = e
                logger.error(f"Key id={key_id} unexpected error: {e}")
                PROVIDER_REQUESTS.labels(
                    provider=provider_name, key_label=str(key_id), status="error"
                ).inc()
                continue

        raise Exception(
            f"All API keys exhausted for model '{request.model}'. "
            f"Tried {len(tried_key_ids)} key(s). Last provider: {last_provider}. Last error: {last_error}"
        )

    def _build_provider(self, provider_name: str, api_key: str, base_url: str | None):
        if provider_name == "groq":
            return GroqProvider(api_key=api_key)
        if provider_name == "openai-compatible":
            if not base_url:
                raise RuntimeError(
                    "OpenAI-compatible key is missing base_url. "
                    "Please set base_url for this key in /admin/keys."
                )
            return OpenAICompatibleProvider(
                api_key=api_key,
                base_url=base_url,
                provider_name=provider_name,
            )
        raise RuntimeError(f"Unsupported provider '{provider_name}'")

    async def route_request_stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        """
        Streaming version of route_request.
        Picks a key and streams SSE chunks from the provider.
        On failure, retries with the next key.
        """
        tried_key_ids: Set[int] = set()
        last_error = None
        last_provider = None

        for attempt in range(20):
            try:
                api_key, key_id, provider_name, base_url, _ = await key_manager.get_available_key_for_model(
                    model=request.model,
                    exclude_key_ids=tried_key_ids,
                    supported_providers=self.supported_providers,
                )
            except RuntimeError as e:
                last_error = e
                break

            if key_id in tried_key_ids:
                break
            tried_key_ids.add(key_id)
            last_provider = provider_name

            if attempt > 0:
                KEY_SWITCHES.inc()
                logger.info(
                    "Switched to key id=%s provider=%s (stream attempt %s)",
                    key_id, provider_name, attempt + 1,
                )

            provider = self._build_provider(
                provider_name=provider_name,
                api_key=api_key,
                base_url=base_url,
            )

            try:
                async for chunk in provider.generate_stream(request):
                    yield chunk
                PROVIDER_REQUESTS.labels(
                    provider=provider_name, key_label=str(key_id), status="success"
                ).inc()
                return
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                last_error = e
                if status == 429:
                    logger.warning(f"Key id={key_id} got 429 (stream) — marking exhausted and retrying")
                    await key_manager.mark_key_exhausted(key_id)
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="rate_limited"
                    ).inc()
                    continue
                elif status == 401:
                    logger.warning(f"Key id={key_id} got 401 (stream) — disabling")
                    await key_manager.toggle_key(key_id, enabled=False)
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="auth_error"
                    ).inc()
                    continue
                elif status >= 500:
                    logger.warning(f"Key id={key_id} got {status} (stream) — trying next key")
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="server_error"
                    ).inc()
                    continue
                else:
                    PROVIDER_REQUESTS.labels(
                        provider=provider_name, key_label=str(key_id), status="client_error"
                    ).inc()
                    raise
            except Exception as e:
                last_error = e
                logger.error(f"Key id={key_id} stream error: {e}")
                PROVIDER_REQUESTS.labels(
                    provider=provider_name, key_label=str(key_id), status="error"
                ).inc()
                continue

        raise Exception(
            f"All API keys exhausted for model '{request.model}' (stream). "
            f"Tried {len(tried_key_ids)} key(s). Last provider: {last_provider}. Last error: {last_error}"
        )
