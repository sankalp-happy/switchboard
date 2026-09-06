# Development reference

For the five-minute "clone to green test run" path, read
[CONTRIBUTING.md](../CONTRIBUTING.md). This page is the reference detail behind it: how the
suite is wired, what CI actually checks, and why the dependency pins look the way they do.

## Layout notes that will bite you

- `gateway/` and `routing/` have **no `__init__.py`** — they are namespace packages.
- There is no root `conftest.py` and `tests/` has no `__init__.py`, so pytest's prepend
  import mode inserts `tests/` rather than the repo root on `sys.path`.
- `pytest.ini` therefore sets `pythonpath = .`. Without it, a bare `pytest` fails at
  `tests/conftest.py` with `ModuleNotFoundError: No module named 'core'`. It appeared to
  work before only because `python -m pytest` silently prepends the current directory,
  which `pytest` does not.

## pytest configuration

`pytest.ini`:

| Setting | Value | Effect |
|---|---|---|
| `addopts` | `-m "not integration"` | The default run excludes tests needing live credentials or Redis |
| `markers` | `integration` | Registered so an unknown-marker warning is a real signal |
| `asyncio_mode` | `strict` | Async tests must be decorated `@pytest.mark.asyncio` |
| `testpaths` | `tests` | |
| `pythonpath` | `.` | See above |

### The two tiers

| Tier | Marker | Needs | Default | CI |
|---|---|---|---|---|
| Unit / API | none | nothing | runs | runs |
| Integration | `integration` | live `GOOGLE_API_KEY` and reachable Redis | skipped | not run |

A bare `pytest` is green on a fresh clone with nothing configured — that is the first
command a new contributor runs, and it must not fail for reasons they did not cause.

CI cannot run the integration tier either: pull requests from forks never receive
repository secrets, and a permanently red check trains everyone to ignore checks. It does
run `pytest -m integration --collect-only -q`, so a syntax error or a bad marker in that
tier still fails the build rather than hiding.

Integration tests also `skipif` themselves when prerequisites are absent, so
`pytest -m integration` on an unconfigured machine reports a skip rather than a failure.

## The conftest bootstrap

`tests/conftest.py`. **Ordering is load-bearing**, because of two module-level side effects:

```
core/config.py    settings = Settings()                       <- reads env at import
gateway/auth.py   _parse_tokens(); validate_auth_config()      <- raises if tokens unset
```

pytest imports `conftest.py` before any test module, so every variable the app needs is set
at the top of the file — above any import that reaches `core.config`. The imports below that
line carry `# noqa: E402` for exactly this reason.

Bootstrapped values: a tempdir `SQLITE_DB_PATH`, a freshly generated `ENCRYPTION_KEY`, empty
provider keys, a default `REDIS_URL`, one admin token, and **two** comma-separated client
tokens — so the rotation path is exercised against the real app config rather than only in
the `_parse_tokens` unit tests. All use `setdefault`, so an externally exported value wins.

If `ADMIN_TOKENS` / `CLIENT_TOKENS` were unset here, `validate_auth_config()` would raise and
the whole suite would fail to collect. That is the fail-closed behaviour working, not a
broken test setup.

### Fixtures

| Fixture | Scope | Purpose |
|---|---|---|
| `_close_db_at_session_end` | session, autouse | Closes the shared aiosqlite connection once at the end, reaping its worker thread so pytest does not hang at exit |
| `clean_db` | function | Truncates the tables between tests. Deliberately does **not** close the singleton connection |
| `stub_router` | function | Monkeypatches the router so tests never make real HTTP calls |
| `admin_client` | function | `AsyncClient` over `ASGITransport` with the admin token attached |
| `client_scope_client` | function | Same with a client-scope token |
| `raw_client` | function | **No** auth header — the point of the whole arrangement. If every client carried credentials, no test could prove an unauthenticated request is rejected |

Note that `ASGITransport` does not run the FastAPI lifespan. That is why
`validate_auth_config()` runs at import rather than in the lifespan: a lifespan check would
be untestable by this suite.

## Test suite map

Ten files, roughly 1900 lines.

| File | Lines | Covers |
|---|---|---|
| `conftest.py` | 152 | Bootstrap and fixtures (above) |
| `test_auth.py` | 316 | Token parsing, fail-closed validation, bearer extraction, the full scope matrix, `WWW-Authenticate` |
| `test_semantic_cache.py` | 255 | The 0.9 threshold admit/reject boundary, disabled-embeddings bypass, similarity header semantics, plus one live `integration` test |
| `test_token_exhaustion.py` | 248 | The `scripts/token_exhaustion.py` load generator itself |
| `test_usage_tracking.py` | 246 | Minute buckets, aggregation windows, cleanup |
| `test_key_manager.py` | 202 | Encryption round-trip, masking, header and duration parsing, selection order, exhaustion, sweeper |
| `test_routing.py` | 189 | Key pick, token metrics by provider and key, 429 switch, all-keys-exhausted |
| `test_admin_api.py` | 133 | Admin CRUD, `/health`, and that `app.version` tracks the `VERSION` file |
| `test_provider_routing.py` | 102 | Per-request provider selection |
| `test_stream_rejection.py` | 67 | `stream: true` returns 400 in the OpenAI error shape |

`test_semantic_cache.py` is the worked example of the two-tier pattern: unit tests patch
`RedisCache._get_embedding` with fixed vectors to exercise the cosine gate deterministically,
while the integration test verifies the real embedding API still agrees with that gate.

If you add a test that needs credentials or a live service, mark it and skip it:

```python
@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("GOOGLE_API_KEY"), reason="needs a live key")
async def test_something_real():
    ...
```

### Coverage

Measured at 68% overall. The thin areas are the provider adapters — Anthropic ~24%, Google
and Groq ~30%, `gateway/main.py` ~47% — which is precisely where failover behaviour lives.
Adapter tests that patch the class in the router registry (as `test_provider_routing.py`
does) are the cheapest way to improve this.

## CI

`.github/workflows/ci.yml`, on push to `main` and on every pull request.
`permissions: contents: read` — nothing here writes to the repo. Concurrency group per ref
with `cancel-in-progress`.

### `test`

Matrix over Python **3.11** (matches the Dockerfile base, so it is what actually ships) and
**3.12** (catches forward-compatibility breaks early). Installs from `requirements-dev.txt`
rather than an inline package list, which makes the job a regression test for the
requirements split itself: if the split breaks a fresh contributor install, it breaks here
first. Runs `pytest -v`, then the integration collect-only check.

### `docs-drift`

Four checks. `README.md` is canonical; `site/docs.html` duplicates several of its sections
and the two have drifted before, so this is enforcement rather than an HTML comment saying
"README is canonical".

1. README contains no unresolved placeholder (`<your-org>`, `YOUR_ORG`, `TODO:`, `FIXME:`).
2. The clone URL agrees between `README.md` and `site/docs.html`.
3. The `VERSION` string appears in `README.md`, `site/docs.html`, and `CHANGELOG.md`.
4. Every path named in the README's "Project Structure" fenced block actually exists.

**These checks do not scan `docs/`.** The pages in this directory therefore avoid version
strings and clone URLs deliberately — adding one would create a drift source that nothing
polices. Note also that check 4 is one-directional: it verifies claimed paths exist, not
that existing paths are claimed, so adding a directory without mentioning it in the README
is safe.

### `docker`

Builds the image, then asserts no `pytest` and no `/app/.env` inside it. See
[deployment.md](deployment.md).

## Dependency policy

Versions in `requirements.txt` are pinned **exactly**. This is deployable application code,
not a library, so reproducibility beats flexibility: a contributor's install resolves to the
same versions CI tested against. Dependabot proposes the upgrades — pip weekly with minor
and patch grouped, github-actions weekly, docker monthly.

`requirements-dev.txt` is `-r requirements.txt` plus `pytest` and `pytest-asyncio`. It is
the only file a contributor needs.

### The numpy ceiling

```
numpy==2.4.6
```

numpy 2.5.0 raised its Python floor to `>=3.12`. The CI matrix tests 3.11 and the Dockerfile
ships `3.11-slim`, so 2.4.x is the last usable line. Dependabot will keep proposing 2.5.x
and it cannot install here. **Lift this pin only together with the Python floor in
`ci.yml` and the `Dockerfile`.**

## Type checking

`pyrightconfig.json` sets a single execution environment rooted at `.` — enough to make
Pyright resolve the namespace-package imports the same way `pythonpath = .` does for pytest.
Pyright is not run in CI; it exists for editor integration.

## Operator scripts

Neither runs as part of the gateway, and neither is in the Docker image. Both are documented
in full in [scripts/README.md](../scripts/README.md).

- `scripts/verify-hardening.sh` — asserts deployment hardening against a running stack.
- `scripts/token_exhaustion.py` — a load generator that drives **your own** gateway hard
  enough to exhaust a provider key, so you can watch rotation happen. It costs real money on
  a paid plan and leaves keys exhausted until their window resets. Only point it at a
  gateway you own.
