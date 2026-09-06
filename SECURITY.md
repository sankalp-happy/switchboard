# Security Policy

SwitchBoard holds provider API keys. It encrypts them at rest with Fernet, stores
them in SQLite, and guards every route except `/health` behind a bearer token. That
makes it a credential custodian, so security reports matter more here than they do
for most small projects. Thank you for taking the time.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting.** Go to the
[Security tab](https://github.com/sankalp-happy/switchboard/security/advisories/new)
and open a draft advisory. It is private between you and the maintainers until we
publish it, and it gives us a place to work on a fix and credit you properly.

**Please do not open a public issue for a security problem.** A public issue tells
everyone running SwitchBoard about the hole at the same moment it tells us.

What helps in a report:

- The version or commit SHA you tested against
- How SwitchBoard was deployed (Docker Compose via `./setup.sh`, or a manual run)
- Steps to reproduce, ideally a `curl` that shows the behaviour
- What an attacker gets out of it

## What to expect

| | |
|---|---|
| First response | Within 7 days |
| Assessment and severity | Within 14 days |
| Fix for a confirmed high-severity issue | Targeted within 30 days |

This is a small project, not a vendor with an on-call rotation. If a deadline
slips we will say so in the advisory thread rather than go quiet.

We will credit you in the advisory and the changelog unless you would rather stay
anonymous.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| < 0.2   | No        |

Only the latest minor release gets fixes. There are no long-term support branches.

## Scope

**In scope**

- Authentication and authorization bypass on `/v1/*`, `/admin/*`, `/metrics`, or `/openapi.json`
- Recovering plaintext provider API keys from the database, logs, metrics, or an error response
- Anything that leaks one caller's data to another caller
- Injection, SSRF, or path traversal reachable from a request
- Weaknesses in the container or Compose defaults that expose secrets

**Out of scope**

- Anything requiring an attacker to already hold a valid `ADMIN_TOKENS` value. Admin
  tokens are full control by design.
- Vulnerabilities in Groq, Google, or Anthropic themselves. Report those to the vendor.
- Denial of service from sending a lot of traffic with a valid client token. Per-caller
  rate limiting is a known gap, tracked in [TODOS.md](docs/TODOS.md).
- Anything that needs an attacker to already have shell access to the host.
- Missing hardening that is documented as a known limitation in the README.

## Known limitations

These are deliberate and documented rather than undiscovered. Reporting them is
welcome but they are already tracked in [TODOS.md](docs/TODOS.md):

- **No per-caller rate limiting.** A valid client token can consume the whole provider quota.
- **The gateway container runs as root.** Deferred over a volume-ownership concern.

## Running SwitchBoard safely

- Never commit `.env`. It is gitignored, and it holds `ENCRYPTION_KEY`, which decrypts
  every stored provider key.
- Do not bind-mount the repo into the gateway container. `docker-compose.yml` explains
  why, and `scripts/verify-hardening.sh` fails if you re-add it.
- Do not expose the gateway directly to the internet without a reverse proxy that
  terminates TLS. Bearer tokens over plain HTTP are readable in transit.
- Rotate `ADMIN_TOKENS` and `CLIENT_TOKENS` by adding the new value, migrating callers,
  then removing the old one. Both settings accept a comma-separated list for exactly this.
