# Observability

Three surfaces: Prometheus metrics, response headers, and logs. There is no tracing.

## Metrics

Defined in `core/metrics.py` using `prometheus_client`. All are process-local and reset when
the gateway restarts.

| Metric | Type | Labels | Incremented at |
|---|---|---|---|
| `switchboard_cache_hits_total` | Counter | — | `gateway/main.py`, on a cache hit |
| `switchboard_cache_misses_total` | Counter | — | `gateway/main.py`, after the cache lookup returns nothing |
| `switchboard_provider_requests_total` | Counter | `provider`, `key_label`, `status` | `routing/router.py`, once per attempt |
| `switchboard_provider_latency_seconds` | Histogram | `provider` | `routing/router.py`, on success only |
| `switchboard_key_switches_total` | Counter | — | `routing/router.py`, on every attempt after the first |
| `switchboard_tokens_processed_total` | Counter | `provider`, `key_label`, `direction` | `routing/router.py`, twice per success |
| `switchboard_active_keys` | Gauge | `provider` | `gateway/main.py` lifespan, **at startup only** |

### Label values

- **`key_label`** is the stringified `api_keys.id`, **not** the human `label` column. The
  name is historical. Correlate it with `GET /admin/stats` to get the readable name.
- **`status`** on `provider_requests` is one of `success`, `rate_limited` (429),
  `auth_error` (401), `server_error` (5xx), `client_error` (other 4xx), or `error` (any
  non-HTTP exception).
- **`direction`** on `tokens_processed` is `input` (prompt tokens) or `output` (completion
  tokens).

### Latency buckets

`switchboard_provider_latency_seconds` uses explicit buckets:
`0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0`. The top bucket matches the adapters' 30-second
HTTP timeout, so anything landing in `+Inf` is a timeout or a client-side failure rather
than a slow response.

Only successful calls are observed. Failed attempts appear in `provider_requests_total` with
a non-`success` status but contribute no latency sample.

### Gauge caveat

`switchboard_active_keys` is set **once**, during startup, from the enabled keys in the
database. Adding, deleting, disabling, or auto-disabling a key does not update it. It also
never reaches zero for a provider that has no enabled keys at startup — the loop that sets
it only iterates providers that actually have enabled keys, so a provider whose last key is
disabled keeps its stale value until the next restart.

### Auto-instrumentation

`prometheus-fastapi-instrumentator` adds standard per-endpoint metrics (request counts,
latency histograms, in-progress gauges) on top of the above, keyed by handler and status
code. This is what powers the "Total Requests" and throughput panels.

## The /metrics endpoint

Mounted behind `require_admin`:

```python
Instrumentator().instrument(app).expose(
    app, endpoint="/metrics", dependencies=[Depends(require_admin)]
)
```

`expose()` forwards `**kwargs` to `app.get()`, which is how the guard attaches without
wrapping the instrumentator's own handler.

**Why it is guarded.** The page is not "just uptime": it carries per-endpoint request counts,
latency histograms, and the active-keys gauge per provider — enough to read off traffic
volume, which providers are live, and how many keys are held. The gateway port is published
to the host on purpose, so an unauthenticated `/metrics` was readable by anything that could
route to the machine.

The nginx config for the admin UI (`vis/default.conf.template`) deliberately does **not**
proxy `/metrics`. The dashboard never fetched it, and the proxy would have been a second
path to the metrics page on the admin-UI port.

## Prometheus

`prometheus/prometheus.yml`: 15-second scrape and evaluation intervals, one job
`switchboard-gateway` targeting `gateway:8000/metrics` with the static label
`service="switchboard"`.

Authentication uses a file, not an inline value:

```yaml
authorization:
  type: Bearer
  credentials_file: /etc/prometheus/scrape_token
```

Prometheus does not expand environment variables in its config, and a live admin token must
not be committed — so `setup.sh` writes `prometheus/scrape_token` (mode 600) from the first
`ADMIN_TOKENS` entry and Compose mounts it read-only.

Two operational consequences:

- **Rotating `ADMIN_TOKENS` breaks scraping** unless you update this file too. The symptom
  is 403s in the Prometheus target page.
- **If the file is missing, Docker creates a directory at that path** and Prometheus fails
  to read it. Run `./setup.sh`; do not create it by hand.

Known gap, flagged inline at `gateway/main.py:233-236`: `/metrics` reuses the admin scope
rather than having a metrics-only one, so the Prometheus container holds a **full admin
credential**. The suggested fix is a `METRICS_TOKENS` scope in `gateway/auth.py`.

## Grafana

Provisioned automatically. Datasource `grafana/provisioning/datasources/prometheus.yml`
points at `http://prometheus:9090` with uid `PBFA97CFB590B2093` — the dashboard JSON
references that uid, so changing it breaks every panel. Dashboards are loaded from
`/var/lib/grafana/dashboards` into a folder named `Switchboard`
(`grafana/provisioning/dashboards/dashboards.yml`).

`grafana/dashboards/switchboard.json` provides nine panels:

| Panel | Reads |
|---|---|
| Cache Hit Rate | hits / (hits + misses) |
| Total Request Throughput | instrumentator request rate |
| Total Requests (counter) | instrumentator request total |
| Cache Hits (counter) | `switchboard_cache_hits_total` |
| Provider Latency (p50 / p95 / p99) | `switchboard_provider_latency_seconds` quantiles |
| Key Switches (rate limit rotations) | `switchboard_key_switches_total` |
| Active Keys | `switchboard_active_keys` |
| Tokens Processed | `switchboard_tokens_processed_total` |
| Cache Hits vs Misses | both counters |

Anonymous access is **off**. Every panel reads provider names, key labels and traffic
volume; that is not public data. Log in as `admin` with `GRAFANA_ADMIN_PASSWORD`.

## Response headers

A poor-man's per-request trace, available to any caller without touching Prometheus:
`X-Cache`, `X-Semantic-Similarity`, `X-Provider`, `X-Latency-Ms`. See
[api-reference.md](api-reference.md) for exact semantics. nginx explicitly passes all four
through to the browser for the dashboard.

## Health

`GET /health` reports `{"status", "version", "cache"}` and always returns HTTP 200 so a
degraded cache does not fail a container healthcheck. The `cache` field is the only signal
that Redis is down — before the startup probe existed, a wrong `REDIS_PASSWORD` was
completely invisible because `redis.from_url()` is lazy and the read path short-circuits
without `GOOGLE_API_KEY`.

**Alert on the field, not the status code.**

## Logging

`logging.basicConfig(level=logging.INFO)` in `gateway/main.py`, and nothing more. Six named
loggers:

| Logger | Notable output |
|---|---|
| `switchboard.gateway` | Request model, cache hit/miss, provider failure, startup and shutdown |
| `switchboard.admin` | (declared; quiet in practice) |
| `switchboard.auth` | Warning per rejected token, with the scope name and never the value |
| `switchboard.key_manager` | Key added, key marked exhausted, sweeper resets, below-threshold fallback |
| `switchboard.router` | Key switches, 429/401/5xx per key, unexpected errors |
| `switchboard.cache` | Embedding errors, malformed entries, cache disabled at startup |

Gaps worth knowing before you run this in production:

- **Plain text, not JSON.** Nothing here is machine-parseable without regex.
- **No correlation or request IDs.** You cannot join a log line to the request that caused
  it, or follow one request across the cache, router, and key manager.
- **No log level configuration.** INFO is hardcoded.
- **Key ids appear in logs**, key values never do.

## Tracing

There is none. No OpenTelemetry, no Jaeger, no span instrumentation of any kind. The nearest
substitute is `X-Latency-Ms` plus the provider latency histogram, which together tell you
how much of a request's time was upstream — and nothing about where the rest went.
