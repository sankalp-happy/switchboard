"""
Router — simple key-availability-based routing.
No cost routing, no strategy matrix. Just:
  1. Pick the Groq key with most remaining quota
  2. Call the provider
  3. Update rate limits from response headers
  4. On 429 / exhaustion -> switch to next key and retry
"""

import logging
import httpx
from typing import Set

from core.schemas import ChatCompletionRequest, ChatCompletionResponse, ProviderResult
from core.key_manager import key_manager
from core.metrics import (
    PROVIDER_REQUESTS,
    PROVIDER_LATENCY,
    KEY_SWITCHES,
    TOKENS_PROCESSED,
)
from providers.groq_provider import GroqProvider

logger = logging.getLogger("switchboard.router")


class Router:
    def __init__(self):
        # For MVP, only Groq is supported
        self.provider_name = "groq"

    async def route_request(self, request: ChatCompletionRequest) -> ProviderResult:
        """
        Try available keys in order of remaining quota.
        Retry with the next key on 429 or rate-limit exhaustion.
        """
        tried_key_ids: Set[int] = set()
        last_error = None

        # Try up to 10 keys (practical upper bound)
        for attempt in range(10):
            try:
                api_key, key_id = await key_manager.get_available_key(self.provider_name)
            except RuntimeError as e:
                raise Exception(str(e)) from e

            if key_id in tried_key_ids:
                # We've cycled through all available keys
                break
            tried_key_ids.add(key_id)

            if attempt > 0:
                KEY_SWITCHES.inc()
                logger.info(f"Switched to key id={key_id} (attempt {attempt + 1})")

            provider = GroqProvider(api_key=api_key)

            try:
                result = await provider.generate(request)
                result.key_id = key_id

                # Update rate limits from response headers
                await key_manager.update_rate_limits(key_id, result.rate_limit_headers)

                # Record metrics
                PROVIDER_REQUESTS.labels(
                    provider=self.provider_name, key_label=str(key_id), status="success"
                ).inc()
                PROVIDER_LATENCY.labels(provider=self.provider_name).observe(
                    result.latency_ms / 1000
                )
                TOKENS_PROCESSED.labels(direction="input").inc(
                    result.response.usage.prompt_tokens
                )
                TOKENS_PROCESSED.labels(direction="output").inc(
                    result.response.usage.completion_tokens
                )

                return result

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                last_error = e

                if status == 429:
                    logger.warning(f"Key id={key_id} got 429 — marking exhausted and retrying")
                    await key_manager.mark_key_exhausted(key_id)
                    PROVIDER_REQUESTS.labels(
                        provider=self.provider_name, key_label=str(key_id), status="rate_limited"
                    ).inc()
                    continue
                elif status == 401:
                    logger.warning(f"Key id={key_id} got 401 — invalid key, disabling and trying next")
                    await key_manager.toggle_key(key_id, enabled=False)
                    PROVIDER_REQUESTS.labels(
                        provider=self.provider_name, key_label=str(key_id), status="auth_error"
                    ).inc()
                    continue
                elif status >= 500:
                    logger.warning(f"Key id={key_id} got {status} — trying next key")
                    PROVIDER_REQUESTS.labels(
                        provider=self.provider_name, key_label=str(key_id), status="server_error"
                    ).inc()
                    continue
                else:
                    # 4xx other than 429/401 — don't retry
                    PROVIDER_REQUESTS.labels(
                        provider=self.provider_name, key_label=str(key_id), status="client_error"
                    ).inc()
                    raise

            except Exception as e:
                last_error = e
                logger.error(f"Key id={key_id} unexpected error: {e}")
                PROVIDER_REQUESTS.labels(
                    provider=self.provider_name, key_label=str(key_id), status="error"
                ).inc()
                continue

        raise Exception(
            f"All API keys exhausted for provider '{self.provider_name}'. "
            f"Tried {len(tried_key_ids)} key(s). Last error: {last_error}"
        )
