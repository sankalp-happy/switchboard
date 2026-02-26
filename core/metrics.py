"""
Prometheus metrics for Switchboard gateway.
"""

from prometheus_client import Counter, Histogram, Gauge

# --- Cache ---
CACHE_HITS = Counter(
    "switchboard_cache_hits_total",
    "Total number of semantic cache hits",
)
CACHE_MISSES = Counter(
    "switchboard_cache_misses_total",
    "Total number of semantic cache misses",
)

# --- Provider ---
PROVIDER_REQUESTS = Counter(
    "switchboard_provider_requests_total",
    "Total requests sent to LLM providers",
    ["provider", "key_label", "status"],
)
PROVIDER_LATENCY = Histogram(
    "switchboard_provider_latency_seconds",
    "Provider response latency in seconds",
    ["provider"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# --- Key rotation ---
KEY_SWITCHES = Counter(
    "switchboard_key_switches_total",
    "Times the router switched to a different API key due to rate limits",
)

# --- Tokens ---
TOKENS_PROCESSED = Counter(
    "switchboard_tokens_processed_total",
    "Total tokens processed",
    ["direction"],  # "input" or "output"
)

# --- Active keys gauge ---
ACTIVE_KEYS = Gauge(
    "switchboard_active_keys",
    "Number of enabled API keys by provider",
    ["provider"],
)
