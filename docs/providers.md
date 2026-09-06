# Providers

Three adapters ship today: Groq, Google, and Anthropic. Each translates between
SwitchBoard's unified schema and one vendor's HTTP API.

There are **no vendor SDKs** in the dependency list. Every adapter talks to its upstream
over raw `httpx`, because Groq and Google both expose OpenAI-compatible REST endpoints and
Anthropic's differences are small enough to handle by hand. (`google-genai` is a dependency,
but it is used only by the semantic cache for embeddings, not by any completion adapter.)

## The interface

`providers/base.py`:

```python
class LLMProvider(ABC):
    async def generate(self, request: ChatCompletionRequest) -> ProviderResult: ...
    async def health_check(self) -> bool: ...
    async def get_cost_per_token(self) -> Dict[str, float]: ...
```

Only `generate` is actually called. `health_check` and `get_cost_per_token` are implemented
by all three adapters but invoked by nothing — no route, no router path, no metric. Treat
them as reserved surface: `get_cost_per_token` in particular returns rough hardcoded
estimates (and zeros for Anthropic) that no code depends on and nobody has verified.

Every adapter takes `api_key` as a constructor argument, falling back to the matching
setting when it is `None`. The router always passes an explicit key from the database; the
settings fallback exists for direct instantiation.

## `ProviderResult`

`core/schemas.py:34`. What `generate` returns.

| Field | Type | Set by |
|---|---|---|
| `response` | `ChatCompletionResponse` | the adapter |
| `provider` | `str` | the adapter, hardcoded to its own name |
| `key_id` | `int` | the **router**, after the call returns |
| `latency_ms` | `float` | the adapter, wall-clock around the HTTP call |
| `rate_limit_headers` | `Dict[str, str]` | the adapter |

`rate_limit_headers` is built identically in all three adapters: every response header whose
lowercased name starts with `x-ratelimit`, passed through verbatim for
`core/key_manager.py` to parse.

## The registry

`routing/router.py:34`:

```python
providers = {
    "groq": GroqProvider,
    "google": GoogleProvider,
    "anthropic": AnthropicProvider,
}
```

Names are matched exactly and case-sensitively against `request.provider` or
`SWITCHBOARD_PROVIDER`. An unknown name raises `ValueError` listing the valid options.

## Adapter details

| | Groq | Google | Anthropic |
|---|---|---|---|
| Endpoint | `https://api.groq.com/openai/v1/chat/completions` | `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` | `https://api.anthropic.com/v1/messages` |
| Auth header | `Authorization: Bearer <key>` | `Authorization: Bearer <key>` | `x-api-key: <key>` |
| Extra headers | — | — | `anthropic-version: 2023-06-01` |
| Payload | request passed through | request passed through | rebuilt, see below |
| Timeout | 30s | 30s | 30s |
| Settings fallback | `GROQ_API_KEY` | `GOOGLE_API_KEY` | `ANTHROPIC_API_KEY` |

### Groq and Google

Both are thin. The payload is `request.model_dump(exclude_unset=True, exclude={"provider"})`
— the vendor never sees SwitchBoard's routing field. The response maps field for field:
`choices[].message.{role,content}`, `choices[].{index,finish_reason}`, and
`usage.{prompt_tokens,completion_tokens,total_tokens}`, each with a defensive default so a
partial response does not raise.

`response.raise_for_status()` is what turns an upstream error into the
`httpx.HTTPStatusError` the router's failover loop is written around.

### Anthropic

The only adapter that genuinely translates. `_build_payload`:

- **Hoists the system message.** Anthropic takes `system` as a top-level string, not a
  message role. The **first** message with `role == "system"` is pulled out into
  `payload["system"]` and removed from `messages`; any subsequent system messages are left
  in the array as-is.
- **Hardcodes `max_tokens: 1024`.** The Anthropic API requires the field, and
  `ChatCompletionRequest` has no equivalent to source it from. This is a real behavioural
  difference: long completions are truncated at 1024 tokens on Anthropic and not on the
  other two.
- Passes `temperature` through when it is not `None`.

Response mapping is also asymmetric:

| Anthropic | Unified |
|---|---|
| `content[0].text` | `choices[0].message.content` |
| `stop_reason` | `choices[0].finish_reason` |
| `usage.input_tokens` | `usage.prompt_tokens` |
| `usage.output_tokens` | `usage.completion_tokens` |
| sum of the two | `usage.total_tokens` (Anthropic sends no total) |

Only the **first** content block is read. Anthropic responses with multiple blocks lose
everything after the first. `created` is set from local wall-clock time, because Anthropic
does not return a creation timestamp.

## Adding a provider

1. **Write the adapter.** New file in `providers/`, subclassing `LLMProvider`. Implement all
   three abstract methods. In `generate`: time the call, `raise_for_status()`, map the
   response into `ChatCompletionResponse`, collect `x-ratelimit*` headers, and return a
   `ProviderResult` with `provider` set to your registry name and `latency_ms` filled in.
2. **Register it.** Add it to the dict at `routing/router.py:34`. The name you use there is
   what callers pass as `provider` in the request body.
3. **Add the setting.** A new `<VENDOR>_API_KEY` field on `Settings` in `core/config.py` if
   you want an environment fallback, plus a line in `.env.example` and the compose
   `environment:` block.
4. **Do not** add a seeding path unless you mean it. Only Groq is auto-seeded from the
   environment (`seed_from_env`); everything else is added through `POST /admin/keys`.
5. **Test it.** Follow `tests/test_provider_routing.py`, which patches the adapter class in
   the registry rather than making real HTTP calls. Cover at minimum: successful mapping,
   the 429 failover path, and the 401 disable path.

Things that need no change: key storage, encryption, rate-limit parsing, metrics, and the
admin API all key off the provider string and work for any name you register.

### If your vendor is not OpenAI-compatible

Follow the Anthropic adapter's shape: keep the translation entirely inside the adapter, in a
`_build_payload` helper, so the unified schema stays vendor-neutral. Where the translation
is lossy — a hardcoded `max_tokens`, a dropped content block — say so in a comment, because
that difference will surface as a support question later.
