# SwitchBoard reference documentation

These pages are the **reference** half of the project's documentation. They describe how
SwitchBoard actually behaves, module by module, with citations into the source so you can
check any claim against the code.

For installation, quickstart, and everyday usage, start with the [root README](../README.md).
For the contributor workflow, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Orientation

A completion request passes through five layers, in this order:

```
POST /v1/chat/completions
   │
   ├─ 1. auth          gateway/auth.py      require_client  → 401 / 403
   ├─ 2. validation    gateway/main.py      stream: true    → 400
   ├─ 3. cache         cache/redis_client.py  embed + cosine scan → HIT, return
   ├─ 4. router        routing/router.py    pick key, failover on 429/401/5xx
   └─ 5. provider      providers/*.py       HTTP call to the vendor
```

Everything else — the admin API, the metrics, the SQLite schema — exists to support
step 4: knowing which keys you have, how much quota each has left, and what happened.

## Pages

| Page | What it covers |
|---|---|
| [architecture.md](architecture.md) | Component map, the request flow and the startup flow, background tasks |
| [api-reference.md](api-reference.md) | Every HTTP route, request/response shapes, response headers |
| [configuration.md](configuration.md) | Every environment variable and hardcoded constant |
| [auth.md](auth.md) | The two token scopes, fail-closed startup, rotation |
| [routing-and-keys.md](routing-and-keys.md) | Key selection, the failover state machine, rate-limit accounting |
| [caching.md](caching.md) | The semantic cache algorithm, its constants, and its limits |
| [providers.md](providers.md) | The provider interface, each adapter, and how to add one |
| [data-model.md](data-model.md) | SQLite schema, Redis payload shape, usage buckets |
| [observability.md](observability.md) | Metrics, Grafana, logging, and what is missing |
| [deployment.md](deployment.md) | Compose topology, hardening decisions, image invariants |
| [development.md](development.md) | Test suite map, test tiers, CI jobs, dependency policy |
| [TODOS.md](TODOS.md) | Known gaps, prioritised, with the context to pick one up cold |

## Conventions used here

- Code is cited as `path/to/file.py:LINE`. Line numbers drift; the symbol name next to
  them is the durable part.
- Constants are quoted with their value and the file that defines them.
- Known limitations link to the matching entry in [TODOS.md](TODOS.md) rather than
  re-arguing the tradeoff here.
