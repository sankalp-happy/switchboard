# SwitchBoard

**A highly available, multi-provider LLM operations gateway with semantic caching, automatic key rotation, and production-grade observability.**

SwitchBoard sits between your client applications and upstream LLM providers, offering a unified OpenAI-compatible API while reducing cost via semantic caching and improving reliability through intelligent multi-key routing with automatic failover.

---

## Features

- **OpenAI-Compatible API** — Drop-in `/v1/chat/completions` endpoint that works with any OpenAI client library.
- **Semantic Caching** — Embedding-based similarity cache (powered by Google Gemini embeddings + Redis) that returns cached responses for semantically similar prompts, saving tokens and latency.
- **Automatic Key Rotation** — Register multiple API keys per provider; the router picks the key with the most remaining quota and automatically fails over on 429 rate limits or auth errors.
- **Encrypted Key Storage** — API keys are encrypted at rest with Fernet (AES-128-CBC) and stored in SQLite.
- **Rate-Limit Awareness** — Parses provider rate-limit headers in real time, tracks per-key quotas, and runs a background sweeper to reset expired windows.
- **Admin API & Dashboard** — Full CRUD for API keys, provider health summaries, and stats via REST endpoints + a retro-industrial web dashboard (static HTML/CSS/JS served by nginx).
- **Prometheus + Grafana Observability** — Pre-configured dashboards tracking cache hit rates, provider latency, key switches, and token throughput.

---

## Tech Stack

| Layer               | Technology                                  |
| ------------------- | ------------------------------------------- |
| API Framework       | FastAPI + Uvicorn                           |
| Cache               | Redis 7 (async via `redis-py`)              |
| Embeddings          | Google Gemini (`gemini-embedding-001`)      |
| LLM Provider        | Groq (OpenAI-compatible)                    |
| Key Storage         | SQLite + Fernet encryption (`cryptography`) |
| Schemas / Validation| Pydantic v2                                 |
| Admin UI            | Static HTML/CSS/JS + nginx (reverse proxy)  |
| Metrics             | Prometheus + Grafana                        |
| HTTP Client         | HTTPX (async)                               |
| Testing             | pytest + pytest-asyncio                     |
| Containerisation    | Docker + Docker Compose                     |
| Language            | Python 3.11                                 |

---

## Architecture

```
┌──────────────┐
│  Client App  │
└──────┬───────┘
       │  POST /v1/chat/completions
       ▼
┌──────────────────────────────────────────────┐
│              SwitchBoard Gateway              │
│                                              │
│  ┌────────────┐   ┌───────────────────────┐  │
│  │  Semantic   │──▶│  Redis (embeddings +  │  │
│  │   Cache     │◀──│   cached responses)   │  │
│  └─────┬──────┘   └───────────────────────┘  │
│        │ miss                                │
│        ▼                                     │
│  ┌────────────┐   ┌───────────────────────┐  │
│  │   Router    │──▶│  Key Manager (SQLite  │  │
│  │  (failover) │◀──│   + Fernet encrypt)   │  │
│  └─────┬──────┘   └───────────────────────┘  │
│        │                                     │
│        ▼                                     │
│  ┌────────────┐                              │
│  │  Provider   │  Groq API                   │
│  │  Adapter    │─────────────────────────▶   │
│  └────────────┘                              │
└──────────────────────────────────────────────┘
       │
       ▼
┌──────────────┐   ┌──────────────┐
│  Prometheus  │──▶│   Grafana    │
└──────────────┘   └──────────────┘
```

---

## Prerequisites

- **Python 3.11+**
- **Docker** and **Docker Compose** (for containerised deployment)
- **Redis** (included via Docker Compose, or run standalone)
- A **Groq** API key (get one at [console.groq.com](https://console.groq.com))
- A **Google AI** API key for embeddings (get one at [aistudio.google.com](https://aistudio.google.com))

---

## Quick Start (Docker Compose)

This is the recommended way to run the full stack — gateway, Redis, admin UI, Prometheus, and Grafana — with a single command.

### 1. Clone the repository

```bash
git clone https://github.com/<your-org>/switchboard.git
cd switchboard
```

### 2. Generate an encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Create a `.env` file

```bash
cp .env.example .env   # or create manually
```

Populate it with:

```env
GROQ_API_KEY=gsk_...
GOOGLE_API_KEY=AIza...
ENCRYPTION_KEY=<key-from-step-2>
```

### 4. Start all services

```bash
docker compose up --build
```

### 5. Verify

| Service         | URL                          |
| --------------- | ---------------------------- |
| Gateway API     | http://localhost:8000        |
| Health Check    | http://localhost:8000/health  |
| API Docs (Swagger) | http://localhost:8000/docs |
| Admin UI        | http://localhost:3000         |
| Prometheus      | http://localhost:9090         |
| Grafana         | http://localhost:3001         |

Grafana default credentials: `admin` / `switchboard`

---

## Local Development (without Docker)

### 1. Create a virtual environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start Redis

```bash
# Using Docker
docker run -d --name switchboard-redis -p 6379:6379 redis:7-alpine

# Or use a locally installed Redis
redis-server
```

### 4. Set environment variables

```bash
export GROQ_API_KEY="gsk_..."
export GOOGLE_API_KEY="AIza..."
export ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export REDIS_URL="redis://localhost:6379/0"
```

### 5. Run the gateway

```bash
uvicorn gateway.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Usage

### Send a Chat Completion Request

SwitchBoard exposes an OpenAI-compatible endpoint, so any standard OpenAI client works:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b-instant",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "temperature": 0.7
  }'
```

**Response headers** include cache and routing metadata:

| Header                  | Description                                         |
| ----------------------- | --------------------------------------------------- |
| `X-Cache`               | `HIT` if served from semantic cache, `MISS` otherwise |
| `X-Semantic-Similarity` | Cosine similarity score of the closest cached prompt |
| `X-Provider`            | The provider that served the request                |
| `X-Latency-Ms`          | Provider response time in milliseconds              |

### Using the Python OpenAI Client

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="not-needed",  # auth is handled by SwitchBoard
)

response = client.chat.completions.create(
    model="llama-3.1-8b-instant",
    messages=[{"role": "user", "content": "Explain quantum computing in one sentence."}],
)
print(response.choices[0].message.content)
```

---

## Admin API

All admin endpoints are mounted under `/admin`. Full interactive docs are available at `/docs`.

| Method   | Endpoint               | Description                                |
| -------- | ---------------------- | ------------------------------------------ |
| `POST`   | `/admin/keys`          | Add a new API key for a provider           |
| `GET`    | `/admin/keys`          | List all keys (masked), filter by provider |
| `DELETE` | `/admin/keys/{key_id}` | Delete a key by ID                         |
| `PATCH`  | `/admin/keys/{key_id}` | Enable or disable a key                    |
| `GET`    | `/admin/providers`     | List providers with key counts and health  |
| `GET`    | `/admin/stats`         | Key totals and rate-limit status           |

### Example: Add a new API key

```bash
curl -X POST http://localhost:8000/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"provider": "groq", "api_key": "gsk_...", "label": "personal-key"}'
```

---

## Observability

### Prometheus Metrics

The gateway auto-exposes metrics at `/metrics`. Key counters and histograms:

| Metric                                  | Type      | Description                          |
| --------------------------------------- | --------- | ------------------------------------ |
| `switchboard_cache_hits_total`          | Counter   | Semantic cache hits                  |
| `switchboard_cache_misses_total`        | Counter   | Semantic cache misses                |
| `switchboard_provider_requests_total`   | Counter   | Requests per provider/key/status     |
| `switchboard_provider_latency_seconds`  | Histogram | Provider response latency            |
| `switchboard_key_switches_total`        | Counter   | Key rotation events                  |
| `switchboard_tokens_processed_total`    | Counter   | Tokens processed (input / output)    |
| `switchboard_active_keys`              | Gauge     | Enabled keys per provider            |

### Grafana

A pre-built dashboard is provisioned automatically when running via Docker Compose. Access it at [http://localhost:3001](http://localhost:3001).

---

## Running Tests

```bash
# Run the full test suite
pytest

# Run a specific test file
pytest tests/test_routing.py

# Run with verbose output
pytest -v
```

Test modules:

| File                        | Coverage Area                        |
| --------------------------- | ------------------------------------ |
| `tests/test_admin_api.py`   | Admin CRUD endpoints                 |
| `tests/test_key_manager.py` | Key encryption, rotation, rate limits|
| `tests/test_routing.py`     | Router failover and key selection    |
| `tests/test_semantic_cache.py` | Embedding-based cache logic       |
| `tests/test_token_exhaustion.py` | Rate-limit exhaustion scenarios |

---

## Project Structure

```
switchboard/
├── gateway/
│   ├── main.py              # FastAPI app, lifespan, /v1/chat/completions
│   └── admin.py             # Admin API router (/admin/*)
├── core/
│   ├── config.py            # Pydantic settings (env vars)
│   ├── database.py          # SQLite async setup
│   ├── key_manager.py       # Key CRUD, encryption, rate-limit tracking
│   ├── metrics.py           # Prometheus metric definitions
│   └── schemas.py           # Pydantic models (request/response)
├── routing/
│   └── router.py            # Key-availability-based routing + failover
├── providers/
│   ├── base.py              # Abstract LLMProvider interface
│   └── groq_provider.py     # Groq (OpenAI-compatible) adapter
├── cache/
│   └── redis_client.py      # Semantic cache (embeddings + Redis)
├── vis/
│   ├── index.html            # Static web dashboard (retro-industrial UI)
│   ├── default.conf.template # nginx config with reverse proxy to gateway
│   └── Dockerfile           # nginx:alpine dashboard container
├── prometheus/
│   └── prometheus.yml       # Prometheus scrape config
├── grafana/
│   ├── dashboards/          # Pre-built Grafana dashboard JSON
│   └── provisioning/        # Auto-provisioning for datasources & dashboards
├── tests/                   # pytest test suite
├── docker-compose.yml       # Full-stack orchestration
├── Dockerfile               # Gateway container
├── requirements.txt         # Python dependencies
└── project_info.md          # Detailed technical documentation
```

---

## Environment Variables

| Variable          | Required | Default                    | Description                                      |
| ----------------- | -------- | -------------------------- | ------------------------------------------------ |
| `ENCRYPTION_KEY`  | Yes      | —                          | Fernet key for encrypting API keys at rest        |
| `GROQ_API_KEY`    | No       | `""`                       | Default Groq key (can also add via Admin API)     |
| `GOOGLE_API_KEY`  | No       | `""`                       | Google AI key for embedding generation            |
| `REDIS_URL`       | No       | `redis://localhost:6379/0` | Redis connection URL                              |
| `SQLITE_DB_PATH`  | No       | `data/switchboard.db`      | Path to the SQLite database file                  |
| `PORT`            | No       | `8000`                     | Gateway listen port                               |
| `HOST`            | No       | `0.0.0.0`                  | Gateway bind address                              |

---

## License

This project is provided as-is for educational and internal use.
