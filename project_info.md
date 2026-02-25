# SwitchBoard : Highly Available LLM Operations Gateway

**Comprehensive Technical Documentation (Day 1 → Production-Ready)**

---

## 1. Vision & System Overview

NexusAI is a **multi-provider LLM gateway** that sits between client applications and upstream LLM providers (OpenAI-compatible APIs, Anthropic-style APIs, or locally hosted models like Llama/Mistral).

Its goals:

* ✅ Reduce cost via **semantic caching**
* ✅ Improve reliability via **intelligent multi-provider routing**
* ✅ Provide a unified, OpenAI-compatible interface
* ✅ Offer production-grade observability and fault tolerance

---

## 2. High-Level Architecture

![Image](https://miro.medium.com/v2/resize%3Afit%3A1400/1%2A7Kfh76Y2ONwbYLpIfOHtVQ.png)

![Image](https://camo.githubusercontent.com/0a8ecc69f4f830a531119514008318ca8b596e21c129ba17e307664fb7f76c68/68747470733a2f2f6769746875622e636f6d2f706572656d61727472612f4c617267652d4c616e67756167652d4d6f64656c2d4e6f7465626f6f6b732d436f757273652f626c6f622f6d61696e2f696d672f73656d616e7469635f63616368652e6a70673f7261773d74727565)

![Image](https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks-microservices/images/microservices-architecture.svg)

![Image](https://www.eksworkshop.com/assets/images/catalog-microservice-eaca1c3f701c42630b93e13e4c2d629a.webp)

### Logical Components

1. **API Gateway Layer**

   * OpenAI-compatible REST + SSE streaming
   * Authentication & rate limiting
   * Request normalization

2. **Semantic Cache Layer**

   * Embedding generation (Sentence-BERT)
   * Vector similarity search (FAISS / Qdrant)
   * Threshold decision logic

3. **Routing Engine**

   * Provider health monitoring
   * Circuit breaker
   * Cost-aware routing logic
   * Context-length-based selection

4. **Provider Abstraction Layer**

   * Unified response schema
   * SSE streaming normalization
   * Error mapping

5. **Observability & Metrics Layer**

   * Cache hit rate
   * Cost savings
   * TTFT
   * TPS

6. **Infrastructure Layer**

   * Kubernetes
   * GPU-backed embedding service
   * Independent scaling

---

# PHASE 1 — DAY 1 TO WEEK 1

## Foundation & MVP (Exact-Match Cache + Static Routing)

---

## 3. MVP Architecture (Minimal Viable Gateway)

### Goal:

* Prove the routing abstraction
* Prove fallback works
* Prove cache works (exact string match)

### MVP Stack

| Layer            | Tech                     |
| ---------------- | ------------------------ |
| API Framework    | FastAPI                  |
| Cache            | Redis                    |
| Provider Adapter | OpenAI-compatible client |
| Routing          | Static priority          |
| Deployment       | Docker                   |
| Testing          | pytest                   |

---

## 4. Request Lifecycle (MVP)

```
Client → FastAPI → Redis (exact match?) 
         → YES → return cached
         → NO → call primary provider
                → if fail → fallback provider
                → store response in Redis
                → return to client
```

---

## 5. Unified API Design

### Endpoint (OpenAI-compatible)

```http
POST /v1/chat/completions
```

### Request Schema

```json
{
  "model": "gpt-4",
  "messages": [...],
  "temperature": 0.7,
  "stream": true
}
```

Internally:

* Map model → provider
* Convert schema if needed
* Normalize response

---

## 6. Provider Abstraction Layer

Each provider implements:

```python
class LLMProvider:
    async def generate(self, request) -> StreamResponse:
        pass

    async def health_check(self) -> bool:
        pass

    async def get_cost_per_token(self) -> float:
        pass
```

Implement:

* `OpenAIProvider`
* `AnthropicProvider`
* `LocalVLLMProvider`

All must return unified schema:

```json
{
  "id": "...",
  "choices": [...],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 456
  }
}
```

---

## 7. Circuit Breaker Design

State machine per provider:

```
CLOSED → (fail threshold reached) → OPEN
OPEN → (cooldown time passed) → HALF_OPEN
HALF_OPEN → success → CLOSED
HALF_OPEN → failure → OPEN
```

Track:

* Failure count
* Rolling error rate
* Cooldown timer

---

# PHASE 2 — WEEK 2 TO WEEK 3

## Semantic Caching (Vector-Based)

---

## 8. Why Semantic Caching?

Exact match caching fails for:

> “Explain neural networks simply.”
> “Can you explain NN in easy terms?”

Semantic embeddings solve this.

---

## 9. Embedding Pipeline

![Image](https://user-images.githubusercontent.com/2041322/49647757-8335a180-fa5e-11e8-9349-b1f8f32a4236.png)

![Image](https://storage.googleapis.com/lds-media/images/cosine-similarity-vectors.original.jpg)

![Image](https://miro.medium.com/1%2A0RYPhRnYxBEUXRhNcKL8Zw.jpeg)

![Image](https://miro.medium.com/1%2A5WnDaQnm9H5vpJGhEQWfOQ.gif)

### Steps:

1. Generate embedding via Sentence-BERT
2. Store:

   * Vector
   * Prompt
   * Response
   * Metadata
3. On new query:

   * Generate embedding
   * Search top-k nearest neighbors
   * Compute cosine similarity
   * If similarity > threshold → return cached

---

## 10. Mathematical Core

### Cosine Similarity

[
sim(A,B) = \frac{A \cdot B}{||A|| ||B||}
]

Where:

* A = incoming embedding
* B = stored embedding

Threshold tuning:

| Threshold | Effect                         |
| --------- | ------------------------------ |
| 0.6       | Too low → hallucinated matches |
| 0.8       | Balanced                       |
| 0.95      | Too strict                     |

Start at **0.85**.

---

## 11. Vector Database Options

| Option | Why                        |
| ------ | -------------------------- |
| FAISS  | Fast, local, good for MVP  |
| Qdrant | Production ready, scalable |
| Milvus | Large-scale distributed    |

Recommended path:

* MVP: FAISS
* Production: Qdrant

---

## 12. Cache Invalidation Strategy

Major problem.

Solutions:

1. TTL-based expiry
2. Versioned cache (prompt template hash)
3. Provider-aware caching
4. Temperature-aware keys

Store metadata:

```json
{
  "embedding": [...],
  "response": "...",
  "temperature": 0.7,
  "model": "gpt-4"
}
```

---

# PHASE 3 — WEEK 4 TO WEEK 5

## Intelligent Routing Engine

---

## 13. Routing Strategy Matrix

| Condition            | Action               |
| -------------------- | -------------------- |
| Short prompt         | Cheap model          |
| Long context         | High-context model   |
| Low latency required | Fast model           |
| Budget constraint    | Lowest cost provider |
| Provider down        | Fallback             |

---

## 14. Dynamic Cost-Aware Routing

Maintain real-time config:

```json
{
  "provider": "openai",
  "cost_per_1k_input": 0.01,
  "cost_per_1k_output": 0.03,
  "latency_ms": 2500,
  "error_rate": 0.02
}
```

Routing score:

[
score = \alpha(cost) + \beta(latency) + \gamma(error_rate)
]

Pick lowest weighted score.

---

## 15. Handling HTTP Failures

* 429 → retry with exponential backoff
* 5xx → immediate fallback
* Timeout → circuit breaker increment

Client never sees provider failure.

---

# PHASE 4 — WEEK 6

## Observability & Metrics

---

## 16. Key Metrics

| Metric              | Why             |
| ------------------- | --------------- |
| Cache Hit Rate      | Cost efficiency |
| TTFT                | UX measurement  |
| TPS                 | Throughput      |
| Provider Error Rate | Reliability     |
| Dollar Savings      | Business impact |

---

## 17. Prometheus Metrics Example

```
nexus_cache_hits_total
nexus_cache_miss_total
nexus_provider_error_total
nexus_tokens_processed_total
nexus_cost_saved_dollars_total
```

Dashboard:

* Grafana
* Alert on:

  * error rate > 5%
  * hit rate < 30%

---

# PHASE 5 — WEEK 7

## Infrastructure & Deployment

---

## 18. Kubernetes Deployment

![Image](https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks-microservices/images/microservices-architecture.svg)

![Image](https://miro.medium.com/1%2AdV7Kec1af1Y1W250Z9FtIA.jpeg)

![Image](https://miro.medium.com/v2/resize%3Afit%3A1400/0%2ArLpXaz7GqPRVOYaI.png)

![Image](https://www.intel.com/content/dam/developer/articles/technical/device-plugins-path-faster-workloads-in-kubernetes/kubernetes-device-plugins.jpg)

### Services:

* nexus-gateway
* embedding-service
* vector-db
* redis
* prometheus
* grafana

---

## 19. Scaling Strategy

Independent scaling:

| Component         | Scaling Metric |
| ----------------- | -------------- |
| Gateway           | RPS            |
| Embedding service | CPU/GPU        |
| Vector DB         | Memory         |
| Redis             | Memory         |

Use:

* HPA
* Node pools with GPU support

---

# PHASE 6 — WEEK 8

## Security & Production Hardening

---

## 20. Secrets Management

Use:

* HashiCorp Vault
* Kubernetes Secrets (encrypted)
* API key rotation

---

## 21. Multi-Tenant Isolation

* Tenant ID required in header
* Per-tenant:

  * Rate limiting
  * Cache shard
  * Cost tracking

---

## 22. Rate Limiting Strategy

* Token bucket per tenant
* Prevent noisy neighbors
* 429 if exceeded

---

# Testing Strategy

---

## 23. Unit Tests

Test:

* Routing logic
* Similarity threshold logic
* Circuit breaker transitions

---

## 24. Load Testing

Use:

* Locust
* k6

Simulate:

* 10k QPS
* Random provider outages
* High cache hit patterns

---

# Measurable SLO Targets

| Metric            | Target |
| ----------------- | ------ |
| Cache hit latency | < 50ms |
| Routing overhead  | < 10ms |
| Availability      | 99.9%  |
| Cost reduction    | 40–70% |

---

# Suggested Repository Structure

```
nexusai/
 ├── gateway/
 ├── providers/
 ├── routing/
 ├── cache/
 ├── embeddings/
 ├── metrics/
 ├── helm/
 ├── tests/
```

---

# Final Deliverable Checklist

✔ OpenAI-compatible API
✔ Semantic caching (vector search)
✔ Dynamic routing
✔ Circuit breaker
✔ Fallback strategy
✔ Prometheus metrics
✔ Kubernetes deployment
✔ Cost dashboard
✔ Load test report
✔ Architecture diagram

---

# Recruiter Impact

This project proves:

* Deep LLMOps understanding
* Distributed system design
* Vector mathematics competence
* Production AI reliability thinking
* Cost engineering mindset
* Kubernetes-level deployment maturity

This is **senior-level AI infrastructure work**, not just an AI app.

---

If you want next, I can:

* Break this into a **precise weekly execution roadmap**
* Or design a **production-ready folder architecture with real config examples**
* Or help you define the **exact tech stack choices to maximize recruiter impact**

# Switchboard – MVP Deployment & Control Plane Architecture

## 1. Product Model (MVP Scope)

Switchboard is a **self-hosted LLM gateway** designed to run locally or within a private infrastructure environment. It acts as a middleware layer between applications and multiple upstream LLM providers.

For the MVP version:

* The system operates in **single-user mode** (no multi-tenancy).
* It is intended to be deployed locally using Docker.
* All provider API keys remain inside the user’s environment.
* Applications connect to Switchboard using an OpenAI-compatible API.

The objective of this phase is to deliver a production-inspired architecture while keeping operational complexity minimal.

---

## 2. High-Level Architecture Overview

The MVP architecture consists of two primary functional areas:

### 2.1 Data Plane (Inference Layer)

Responsible for handling all LLM traffic.

Components include:

* FastAPI Gateway
* Exact-match Redis Cache
* Semantic Cache (Vector Database)
* Embedding Generator
* Provider Adapters (e.g., OpenAI, Anthropic)

This layer handles:

* Request routing
* Cache evaluation
* Fallback logic
* Cost tracking
* Streaming responses

The gateway itself remains stateless. All persistent data is stored in external services.

---

### 2.2 Control Plane (Administrative Interface)

A web-based Admin UI is introduced in the MVP to allow runtime configuration without modifying environment files.

The Control Plane allows the user to:

* Add multiple provider API keys
* Enable or disable providers
* Configure provider rate limits
* Adjust routing preferences
* Monitor provider health status

This eliminates the need to hardcode credentials or restart the system when updating configuration.

---

## 3. Local Development Deployment

Switchboard MVP is designed to run fully on localhost using Docker Compose.

### Services Included

* `switchboard-api` (Gateway) – Port 8000
* `switchboard-ui` (Admin Interface) – Port 3000
* `redis` (Exact Cache)
* `vector-db` (Semantic Cache)
* `postgres` (Configuration Store)
* `prometheus` (Metrics Collection)
* `grafana` (Metrics Visualization)

### Access Points

* API Endpoint:
  `http://localhost:8000/v1`

* Admin UI:
  `http://localhost:3000`

* Metrics Dashboard (Grafana):
  `http://localhost:3001`

This setup mirrors a realistic infrastructure deployment while remaining simple to operate.

---

## 4. Provider Configuration & Secret Management

The MVP introduces dynamic provider management through the Admin UI.

### Configuration Capabilities

Users can:

* Add multiple provider API keys
* Set rate limits per provider
* Toggle providers on or off
* Modify routing weights

### Secret Handling

Provider API keys are:

* Stored securely in a database
* Encrypted before persistence
* Never exposed to the frontend after submission

This ensures that:

* Credentials are not stored in plaintext
* No hardcoded secrets exist in source files
* Configuration can be modified without restarting the service

---

## 5. Usage Dashboard & Observability

Switchboard includes observability as a first-class feature.

### Metrics Collected

* Cache hit rate
* Provider latency
* Error rates
* Token usage
* Estimated cost savings

Metrics are collected using Prometheus and visualized through Grafana.

This provides:

* Real-time system visibility
* Historical performance trends
* Insight into routing effectiveness
* Clear demonstration of cost optimization

For MVP, Grafana serves as the primary dashboard solution rather than building a fully custom analytics frontend.

---

## 6. Application Integration

From an application’s perspective, integration requires only changing the base URL.

Before Switchboard:

```python
OpenAI(api_key="sk-xxx")
```

After Switchboard:

```python
OpenAI(
  base_url="http://localhost:8000/v1"
)
```

No SDK changes are required.

The gateway remains fully compatible with the OpenAI REST and streaming interface.

---

## 7. Stateless Gateway Design (MVP)

Switchboard’s API layer does not store state internally.

All state is externalized to:

* Redis (exact cache)
* Vector Database (semantic cache)
* Postgres (configuration store)
* Prometheus (metrics storage)

This design allows:

* Safe horizontal scaling in future phases
* Independent scaling of components
* Clean separation between control and data layers

---

## 8. Scope Definition for MVP

The MVP intentionally excludes:

* Multi-tenancy
* Per-user cost isolation
* Advanced dynamic routing policies
* Distributed circuit breaker synchronization

The focus of this phase is:

* Stable gateway functionality
* Dynamic provider configuration
* Reliable caching
* Clear observability
* Clean local deployment model

---

## 9. Architectural Positioning

With the introduction of the Admin UI and Dashboard, Switchboard evolves from a simple API proxy into:

> A self-hosted LLM traffic control layer with runtime configuration and production-style observability.

Even in single-user mode, the architecture reflects scalable design principles while maintaining practical scope control.

