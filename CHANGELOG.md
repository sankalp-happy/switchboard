# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`stream: true` is now rejected with HTTP 400** instead of being silently ignored.
  The gateway returns an OpenAI-compatible error (`{"error": {"message": "...", "type": "unsupported_parameter", "param": "stream"}}`)
  when a client sends `stream: true`. The `payload["stream"] = False` lines in
  `providers/groq_provider.py` and `providers/google_provider.py` have been removed
  since the gateway now catches this before reaching the providers.

### Added

## [0.2.0] — 2026-08-22

First release with a license. SwitchBoard's repository was already public but carried no
`LICENSE` file, which meant it was legally unusable despite the README inviting people to
clone it. This release fixes that and makes the project contributor-ready.

The version number is 0.2.0 rather than 0.1.0 because `/health` has been reporting
`0.2.0` for some time; this adopts what the API already claimed rather than regressing it.

### Added

- **`LICENSE` — MIT.** The project is now legally usable, forkable, and deployable.
  Contributions are accepted under the same license, stated explicitly in
  `CONTRIBUTING.md` since MIT has no inbound-contribution clause of its own.
- **`CONTRIBUTING.md`** — clone to passing test run in about five minutes, with no API
  keys and no Redis. Documents the two test tiers.
- **`SECURITY.md`** — private vulnerability reporting via GitHub advisories, response
  expectations, and an explicit scope. Previously a researcher had nowhere to send a
  report except a public issue.
- **`CODE_OF_CONDUCT.md`** — Contributor Covenant 2.1.
- **Continuous integration** (`.github/workflows/ci.yml`) — the test suite runs on
  Python 3.11 and 3.12 for every pull request. It also checks that `README.md` and
  `site/docs.html` have not drifted, that every path named in the README's Project
  Structure actually exists, that the Docker image builds, and that the production image
  contains neither `pytest` nor `.env`.
- **Issue and pull request templates**, plus Dependabot for pip, GitHub Actions, and
  Docker.
- **`pytest.ini`** — registers an `integration` marker and excludes it by default, so a
  bare `pytest` is green on a fresh clone with nothing configured.
- **`requirements-dev.txt`** — test dependencies split out of the runtime set. The
  production image no longer ships a test runner.
- **`VERSION`** — single source of truth for the release number, read by `/health` and
  asserted by CI against the README, the docs site, and this file.
- **`CHANGELOG.md`** — this file.
- **`scripts/README.md`** — documents both operator scripts, including what
  `token_exhaustion.py` costs to run.
- **Unit tests for the semantic cache threshold.** The 0.9 cosine gate, the
  no-credentials bypass path, and the empty-prompt short circuit are now covered with
  mocked embeddings, so they run in CI without a Google API key.
- **Tests for `scripts/token_exhaustion.py`** — payload generation, argument parsing,
  and 429-then-recovery rotation detection over a mocked transport.
- **README sections: Data handling, Limitations, Contributing.** Data handling states
  plainly that semantic caching sends prompt text to Google and retains it in Redis for
  an hour, and that leaving `GOOGLE_API_KEY` unset disables it.

### Changed

- **`/health` reads the version from `VERSION`** instead of a hardcoded string literal.
- **Dockerfile base image is pinned to a digest** rather than the floating
  `python:3.11-slim` tag.
- **`scripts/token_exhaustion.py` rewritten.** It generates its context instead of
  reading a file that was never committed, sends the `Authorization` headers the gateway
  has required since authentication landed, takes `--url` / `--requests` /
  `--context-tokens` rather than hardcoded constants, uses neutral prompts, and reports
  "gateway unreachable" instead of advising you to send more requests when nothing is
  listening.
- **`.dockerignore`** excludes tests, scripts, the marketing site, and project docs from
  the gateway image.
- **README corrections** from a full claim audit against the source:
  - The Quick Start clone command was a literal `<your-org>` placeholder.
  - Project Structure listed a `project_info.md` that does not exist, and omitted
    `google_provider.py`, `anthropic_provider.py`, `auth.py`, `site/`, and `scripts/`.
  - The Admin API table was missing `GET /admin/keys/usage`.
  - The `curl` example passed `$ADMIN_TOKENS` as a bearer token, but that variable is a
    comma-separated list — with more than one token configured it would 403.
  - `/metrics` and `/openapi.json` are documented as admin-guarded, which they have been
    since the hardening work but the README did not say.
  - The "drop-in OpenAI-compatible" claim is scoped to what is actually implemented.

### Removed

- **Three unused dependencies.** `streamlit` and `groq` were imported nowhere in the
  tree, and `requests` existed only for the one script, which now uses the `httpx`
  already required at runtime. Runtime dependencies are pinned exactly.

### Security

- **`stream: true` now returns HTTP 400** with an OpenAI-compatible error payload
  instead of being silently downgraded to a non-streamed response.

## [0.1.0]

Pre-release development. No published release; see the git history.

[Unreleased]: https://github.com/sankalp-happy/switchboard/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sankalp-happy/switchboard/releases/tag/v0.2.0
