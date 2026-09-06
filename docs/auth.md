# Authentication

All of this lives in `gateway/auth.py`. It is a deliberately small module: two token sets,
two dependencies, and one rule connecting them.

## Why a separate module

`gateway/main.py` imports `admin_router` from `gateway/admin.py`. If the guard lived in
`main.py`, then `admin.py` importing it would be a circular import. So the guard lives in a
third module that both can depend on.

## The two scopes

| Setting | Guards | Dependency |
|---|---|---|
| `ADMIN_TOKENS` | `/admin/*`, `/openapi.json`, `/metrics` | `require_admin` |
| `CLIENT_TOKENS` | `/v1/*` | `require_client` |

**Admin is a superset of client.** `require_client` accepts `CLIENT_TOKENS | ADMIN_TOKENS`
(`gateway/auth.py:146`). The reverse is not true: a client token is rejected by
`require_admin`, so a client can never read or modify your provider keys.

The superset rule exists so the admin dashboard can hold a single credential and still use
its test-chat panel, which calls `/v1/chat/completions`.

```
Authorization: Bearer <token>
          |
          v
  _extract_bearer()  --> absent / not "Bearer x" / empty --> 401
          |
          v
  require_admin    token in ADMIN                --> pass, else 403
  require_client   token in CLIENT union ADMIN   --> pass, else 403
```

## Token parsing

`_parse_tokens(raw)` splits on commas, strips surrounding whitespace, drops empty entries,
and collapses duplicates into a `frozenset`. `""` becomes `frozenset()`, which is what makes
the startup validation below able to detect an unset variable.

Both sets are parsed **once at import** (`gateway/auth.py:61-62`), never per request. The
consequence is that changing `ADMIN_TOKENS` or `CLIENT_TOKENS` requires a restart.

## Fail-closed startup

`validate_auth_config()` runs at import (`gateway/auth.py:96`) and raises `RuntimeError` if
either token set is empty:

```
ADMIN_TOKENS and CLIENT_TOKENS not set. The gateway refuses to start without
authentication configured. Run ./setup.sh to generate tokens, or generate one
yourself with: python -c "import secrets; print(secrets.token_urlsafe(32))"
```

An unset token must never silently mean "no authentication". This mirrors the
`ENCRYPTION_KEY` contract in `core/key_manager.py` — one rule for every required secret.

It runs at **import** rather than in the FastAPI lifespan for a testability reason: httpx's
`ASGITransport` does not run lifespan, so a lifespan check would leave the fail-closed
behaviour untested by the API test suite. Import time is still startup.

The function takes optional `admin_tokens` / `client_tokens` arguments so it can be tested
directly without reloading the module.

## Constant-time comparison

```python
def _token_matches(provided: str, allowed: frozenset) -> bool:
    matched = False
    for token in allowed:
        if secrets.compare_digest(provided, token):
            matched = True
    return matched
```

Two things are deliberate here. `provided in allowed` would be a plain hash lookup and is
not constant-time, and `compare_digest` cannot take a set — hence the loop. And the loop
does **not** early-exit on a match, so the time taken does not reveal the position of the
matching token within the set.

## Status codes

| Code | Condition | Response |
|---|---|---|
| 401 | Header absent, not `Bearer <x>`, or the token part is empty | `detail` explains the expected format; includes `WWW-Authenticate: Bearer` |
| 403 | Well-formed bearer token that is not in the allowed set | `{"detail": "Invalid credentials"}` |

The distinction is meaningful: 401 says "you did not authenticate", 403 says "you did, and
you are not allowed". Rejected tokens are logged at warning level with the scope name, never
with the token value.

## The unauthenticated surface

Exactly two routes:

- `GET /health` — intentional. Docker healthchecks and orchestration depend on it, and it
  discloses only a version string and a Redis reachability flag.
- `GET /docs` — a static Swagger shell containing no data. A browser navigating to a URL
  cannot send an `Authorization` header, so guarding the page itself would make it
  unloadable. Everything of value is behind `/openapi.json`, which is admin-guarded.

## Rotating a token

Both settings are comma-separated lists specifically to make this a no-downtime operation:

1. Append a newly generated token: `ADMIN_TOKENS=old_token,new_token`.
2. Restart the gateway. Both are now accepted.
3. Migrate callers to the new token.
4. Remove the old one and restart again.

Generate tokens with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

If you rotate `ADMIN_TOKENS`, also update `prometheus/scrape_token`, which holds the first
entry — otherwise Prometheus scrapes will start returning 403.

## Known gaps

- **No per-caller rate limiting.** Authentication answers "may you call this?" but not
  "how much?". A valid client token can consume your entire provider quota. Tracked in
  [TODOS.md](TODOS.md) at P2.
- **The semantic cache is not caller-isolated.** With more than one `CLIENT_TOKENS` entry,
  caller B can receive a response cached for caller A. Tracked in [TODOS.md](TODOS.md)
  at P1; see [caching.md](caching.md).
- **Prometheus holds a full admin credential.** `/metrics` reuses the admin scope rather
  than having a metrics-only one, so the Prometheus container can also call `/admin/*`. The
  suggested fix, noted inline at `gateway/main.py:233-236`, is a `METRICS_TOKENS` scope.
