# Contributing to SwitchBoard

Thanks for considering it. This guide should get you from `git clone` to a passing
test run in about five minutes, with no API keys and no Redis.

## Licensing of contributions

SwitchBoard is released under the [MIT License](LICENSE). **By submitting a pull
request you agree that your contribution is licensed under the MIT License**, the
same terms as the rest of the project. There is no CLA to sign and no separate
paperwork.

If you are contributing on behalf of an employer, make sure you have their
permission first.

## Getting set up

You do **not** need Docker, Redis, or any provider API key to work on most of
SwitchBoard.

```bash
git clone https://github.com/sankalp-happy/switchboard.git
cd switchboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

That last command should print something like `118 passed, 1 deselected`. If it
does not, that is a bug in the project and not in your setup — please open an issue.

`requirements-dev.txt` pulls in `requirements.txt`, so it is the only file you need.
The production image installs `requirements.txt` alone, which keeps the test runner
out of the deployed container.

## Running the tests

```bash
pytest                      # the default suite. No credentials needed.
pytest -v                   # same, verbose
pytest tests/test_auth.py   # one file
pytest -m integration       # the opt-in suite (see below)
```

### The two test tiers

Tests are split by whether they need something you have to go and get.

| Tier | Marker | Needs | Runs by default? | Runs in CI? |
|---|---|---|---|---|
| Unit / API | none | nothing | Yes | Yes |
| Integration | `integration` | a live `GOOGLE_API_KEY` and a reachable Redis | No | No |

`pytest.ini` sets `addopts = -m "not integration"`, so a bare `pytest` skips the
second tier. This is deliberate: CI cannot run them either, because pull requests
from forks never receive repository secrets, and a permanently red check trains
everyone to ignore checks.

Integration tests also `skipif` themselves when their prerequisites are missing, so
`pytest -m integration` on a machine with no key reports a skip rather than a failure.

**If you add a test that needs credentials or a live service, mark it:**

```python
@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("GOOGLE_API_KEY"), reason="needs a live key")
async def test_something_real():
    ...
```

And where you can, add a mocked unit test alongside it that covers the same logic.
`tests/test_semantic_cache.py` is the worked example: the unit tests patch
`RedisCache._get_embedding` with fixed vectors to exercise the 0.9 cosine gate, while
the integration test verifies the real embedding API still agrees with that gate.

### Async tests

The suite uses `pytest-asyncio` in strict mode. Decorate async tests with
`@pytest.mark.asyncio`.

`tests/conftest.py` sets every environment variable the app reads **before** importing
anything that reaches `core.config`. That ordering is load-bearing and the file explains
why — read the docstring before adding imports near the top of it.

## Running the full stack

```bash
cp .env.example .env
./setup.sh
```

`setup.sh` generates the secrets, remaps host ports if something already holds them, and
brings up the Compose stack. See the [README](README.md) for what ends up on which port.

## Making a change

1. Branch off `main`.
2. Make the change. Add tests — the suite is the contract.
3. Run `pytest` and make sure it is green.
4. Open a pull request and fill in the template.

CI runs the default suite on Python 3.11 and 3.12, checks that the README and the docs
site have not drifted, and builds the Docker image. All of it must pass before merge.

### Things that will get a PR sent back

- New code with no tests.
- A test marked `integration` when it could have been mocked instead.
- Adding a dependency that the standard library or an already-installed package covers.
  `requirements.txt` is pinned and deliberately small; three dead dependencies were
  removed to get it that way.
- Re-adding the `.:/app` bind-mount to `docker-compose.yml`. It carried `.env` into the
  container next to the database it decrypts. `scripts/verify-hardening.sh` fails if you
  do, and the compose file explains the reasoning.
- Editing `site/docs.html` without making the matching change in `README.md`. The README
  is canonical; a CI check compares the parts that must agree.

## Code style

There is no enforced formatter yet. Match the file you are editing.

The codebase leans on explanatory comments more than most — see `gateway/auth.py` or the
volume comment in `docker-compose.yml`. If a decision took you more than a minute to
reason through, write down why, not what. ASCII diagrams for non-obvious flow are welcome
and there are several already.

If you change code that has an ASCII diagram near it, update the diagram in the same
commit. A stale diagram is worse than no diagram.

## Reporting bugs and requesting features

Use the [issue templates](https://github.com/sankalp-happy/switchboard/issues/new/choose).

**Security issues do not go in the issue tracker.** See [SECURITY.md](SECURITY.md) for
the private reporting path.

## Looking for something to work on

[`TODOS.md`](docs/TODOS.md) tracks known gaps with enough context to pick one up cold — what
the problem is, why it matters, which files are involved, and where to start. The test
coverage items are a reasonable first contribution.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
