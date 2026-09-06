# Deployment

For the one-command install, see the [root README](../README.md). This page documents what
the stack actually is, why it is shaped the way it is, and what it does not do.

## Topology

Five Compose services.

| Service | Image / build | Host port | Purpose |
|---|---|---|---|
| `gateway` | build `.` | `${GATEWAY_PORT:-8000}` -> 8000 | The FastAPI app |
| `redis` | `redis:7-alpine` | **none** | Semantic cache |
| `admin-ui` | build `./vis` | `${ADMIN_UI_PORT:-3000}` -> 3000 | nginx: static dashboard plus reverse proxy |
| `prometheus` | `prom/prometheus:latest` | `${PROMETHEUS_PORT:-9090}` -> 9090 | Scrapes the gateway |
| `grafana` | `grafana/grafana:latest` | `${GRAFANA_PORT:-3001}` -> 3000 | Dashboards |

Dependency order: `admin-ui` -> `gateway` -> `redis`, and `grafana` -> `prometheus` ->
`gateway`. These are `depends_on` only — start order, not readiness.

One named volume, `switchboard-data`, mounted at `/app/data` in the gateway. This is the
SQLite database and the only durable state. It survives `docker compose down` and is not
touched by `setup.sh`.

## The gateway image

`Dockerfile`, from `python:3.11-slim` **pinned by digest**
(`sha256:9c900dea...`) rather than by tag, so a rebuild next month gets the same base CI
tested against. Dependabot's docker ecosystem proposes the bumps; note that
`.github/dependabot.yml` explicitly records that it does **not** track this digest pin.

```
mkdir /app/data
COPY requirements.txt .          <- only requirements.txt, never requirements-dev.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV IN_DOCKER=true
CMD uvicorn gateway.main:app --host 0.0.0.0 --port 8000
```

The dependency install is a separate layer from `COPY . .` so a code change does not
reinstall dependencies.

`.dockerignore` keeps `.env`, `.env.*` (but not `.env.example`), `prometheus/scrape_token`,
`data/`, `*.db`, `.env.backup.*` and `.env.snapshot.*` out of the build context.

### Invariants CI asserts

The `docker` job in `.github/workflows/ci.yml` builds the image and then proves two things
rather than trusting them:

1. `pip show pytest` finds nothing — the production image ships no test runner. This is the
   entire point of the `requirements.txt` / `requirements-dev.txt` split.
2. `/app/.env` does not exist — no secrets baked into the image.

## Hardening decisions

Each of these is a deliberate absence, and each has an inline comment in the file explaining
why. `scripts/verify-hardening.sh` asserts several of them against a running stack; run it
after any change to `docker-compose.yml`, `vis/default.conf.template`, or the auth layer.

### The repo is not bind-mounted into the gateway

A `- .:/app` mount carried `.env` into the container, which put `ENCRYPTION_KEY` next to the
database it decrypts — any code execution in the gateway would read every provider key and
every token, defeating the encryption at rest entirely. `.dockerignore` does not apply to
bind mounts, so it could not be excluded. The Dockerfile already `COPY`s the app in and the
CMD has no `--reload`, so the mount bought nothing but a skipped rebuild.

For hot-reload development, put the mount in an untracked `docker-compose.override.yml`,
which Compose merges automatically for a local `up`.

### Redis is not published to the host

The gateway reaches it at `redis:6379` over the Compose network. Publishing it exposed the
semantic cache — every prompt and every response — to anything that could route to the
machine. It is also password-protected via `--requirepass`.

### Required variables use the `:?` form

```yaml
command: redis-server --requirepass ${REDIS_PASSWORD:?REDIS_PASSWORD is required - run ./setup.sh to generate one}
```

Compose aborts with a named error rather than substituting empty. Without this,
`REDIS_PASSWORD` unset produces `redis-server --requirepass` with a missing argument and a
container that exits on startup; `GRAFANA_ADMIN_PASSWORD` unset would be silently accepted
by Grafana as "no password set".

### Grafana anonymous access is off

Grafana is published on the host, and every dashboard reads provider names, key labels and
traffic volume.

### No CORS middleware

nginx serves the dashboard *and* reverse-proxies `/admin/`, `/v1/`, `/health` to the
gateway, and `vis/index.html` sets `API_BASE = ''`. Every dashboard call is therefore
same-origin, and browsers never apply CORS to same-origin requests — so the middleware was
protecting nothing, while `allow_origins=["*"]` with `allow_credentials=True` advertised the
opposite.

If the dashboard is ever served from a different host than the gateway, add `CORSMiddleware`
back with an explicit origin list. Never a wildcard, and never a wildcard together with
credentials.

### nginx does not proxy `/metrics`

The dashboard never fetched it, and the proxy was a second anonymous path to the metrics
page on the admin-UI port. The four cache and routing response headers *are* explicitly
passed through (`proxy_pass_header X-Cache` and friends) so the dashboard can display them.

## setup.sh

One command, idempotent, safe to re-run. Existing `.env` values are reused and never
overwritten; the file is backed up only when something actually changes.

Responsibilities:

- Verify the Docker install.
- Generate `ENCRYPTION_KEY` (Fernet), `ADMIN_TOKENS`, `CLIENT_TOKENS`, `REDIS_PASSWORD`,
  `GRAFANA_ADMIN_PASSWORD`.
- Prompt for any provider keys you want to seed.
- Write `prometheus/scrape_token` (mode 600) from the first `ADMIN_TOKENS` entry.
- Detect host port conflicts and **remap** rather than fighting for the port — it never kills
  another process. Chosen ports are persisted to `.env` so URLs stay stable.
- Clear orphaned containers from an interrupted earlier run
  (`docker compose down --remove-orphans`). The data volume is never touched.
- Bring the stack up and wait for every service to report healthy.
- On failure: write `setup-failure-<timestamp>.log` **first**, then roll back the containers
  it started, so a failed install leaves no ports occupied and you still have the evidence.

Flags: `--minimal` (gateway + Redis + admin UI only), `--yes`, `--rebuild`, `--dry-run`,
`--keep-on-failure`, `--no-color`.

## Verifying a deployment

```bash
curl http://localhost:8000/health          # no token needed
./scripts/verify-hardening.sh              # reads ADMIN_TOKENS and CLIENT_TOKENS from env
```

`verify-hardening.sh` asserts the repo is not bind-mounted into the gateway container, that
`/metrics` refuses a client token, that Grafana requires a login, and that CORS does not
send credentials cross-origin.

## Upgrading

The database schema is created and migrated by `init_db()` at startup, so upgrading is
pull-and-restart. Note that a new column added to `SCHEMA_SQL` alone will **not** appear in
an existing database — see the migration section of [data-model.md](data-model.md).

`GRAFANA_ADMIN_PASSWORD` became required after the initial release; a bare
`docker compose up` on an older `.env` aborts with a named error until it is set. Re-run
`./setup.sh`, which keeps every value already present.

## Known gaps

| Gap | Detail |
|---|---|
| **The gateway container runs as root** | No `USER` directive. Deferred over named-volume ownership on `/app/data`; tracked P3 in [TODOS.md](TODOS.md) |
| **Prometheus and Grafana float on `:latest`** | The only unpinned images in the stack. A rebuild can pick up a new major version |
| **SQLite means one writer** | Fine for a single gateway instance; not a horizontally-scalable configuration store. Running two gateway replicas against one volume is not supported |
| **No Kubernetes or Helm** | Compose is the only supported orchestration |
| **No release automation** | CI has no release job. Releases are manual: bump `VERSION`, update `CHANGELOG.md`, tag |
| **`depends_on` without healthchecks** | Start order only. The gateway can begin before Redis accepts connections; the startup probe logs it and the gateway serves without a cache |
| **Prometheus holds a full admin token** | See [observability.md](observability.md) |
