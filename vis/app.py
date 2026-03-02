import streamlit as st
import requests
import redis
import time
import os

# Configuration
API_URL = os.getenv("API_URL", "http://localhost:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

st.set_page_config(page_title="SwitchBoard Dashboard", page_icon="🎛️", layout="wide")

st.title("🎛️ SwitchBoard MVP Dashboard")
st.markdown("Test your multi-provider LLM gateway and observe caching in action.")

# --- Sidebar: System Status ---
st.sidebar.header("System Status")

# Backend Status
backend_connected = False
try:
    health_res = requests.get(f"{API_URL}/health", timeout=2)
    if health_res.status_code == 200:
        backend_connected = True
except Exception:
    backend_connected = False

if backend_connected:
    st.sidebar.success("✅ Backend: Connected")
else:
    st.sidebar.error("❌ Backend: Disconnected")


# Redis Status
redis_connected = False
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    redis_connected = True
except Exception:
    redis_connected = False

if redis_connected:
    st.sidebar.success("✅ Redis: Connected")
else:
    st.sidebar.error("❌ Redis: Disconnected")


# --- Navigation ---
tab_query, tab_keys = st.tabs(["🧪 Test Query", "🔑 API Key Management"])


# =====================================================================
# TAB 1 — Query Testing
# =====================================================================
with tab_query:
    st.subheader("Test API Query")

    model = st.selectbox("Select Model", ["llama-3.1-8b-instant", "llama3-70b-8192", "mixtral-8x7b-32768"])
    system_prompt = st.text_input("System Prompt", "You are a helpful assistant.")
    user_prompt = st.text_area("User Prompt", "What is the capital of France?")

    if st.button("Send Query", type="primary"):
        if not backend_connected:
            st.error("Cannot query: Backend is not connected.")
        else:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.7,
                "stream": False
            }

            start_time = time.time()
            try:
                with st.spinner("Processing query..."):
                    response = requests.post(f"{API_URL}/v1/chat/completions", json=payload)
                end_time = time.time()
                latency = end_time - start_time

                if response.status_code == 200:
                    data = response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

                    # Check cache vs model via header, fallback to latency heuristic
                    x_cache = response.headers.get("X-Cache")
                    x_semantic_similarity = response.headers.get("X-Semantic-Similarity")
                    if x_cache:
                        is_cached = (x_cache == "HIT")
                    else:
                        is_cached = latency < 0.2

                    # Display Results
                    st.markdown("### Response")
                    st.info(content)

                    # Display Details
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Latency", f"{latency:.3f} s")

                    if is_cached:
                        col2.metric("Source", "Cache ⚡")
                    else:
                        col2.metric("Source", "Model 🧠")

                    col3.metric("Tokens used", data.get("usage", {}).get("total_tokens", 0))

                    if x_semantic_similarity and float(x_semantic_similarity) > -1.0:
                        col4.metric("Similarity", x_semantic_similarity)
                    else:
                        col4.metric("Similarity", "N/A")

                    st.expander("Raw API Response").json(data)

                else:
                    st.error(f"API Error ({response.status_code}): {response.text}")

            except Exception as e:
                st.error(f"Request failed: {str(e)}")


# =====================================================================
# TAB 2 — API Key Management
# =====================================================================
with tab_keys:
    st.subheader("API Key Management")
    st.markdown("Add, view, enable/disable, and delete API keys used by the gateway for provider routing.")

    if not backend_connected:
        st.warning("Backend is not connected. Key management requires a running backend.")
    else:
        # ---------- Add New Key ----------
        st.markdown("### ➕ Add New API Key")
        with st.form("add_key_form", clear_on_submit=True):
            ak_col1, ak_col2 = st.columns(2)
            with ak_col1:
                new_provider = st.selectbox(
                    "Provider",
                    ["groq", "openai", "anthropic", "local"],
                    help="The LLM provider this key belongs to.",
                )
            with ak_col2:
                new_label = st.text_input(
                    "Label (optional)",
                    placeholder="e.g. prod-key-1",
                    help="A human-readable label to identify this key.",
                )
            new_api_key = st.text_input(
                "API Key",
                type="password",
                placeholder="sk-...",
                help="The raw API key. It will be encrypted before storage.",
            )
            submitted = st.form_submit_button("Add Key", type="primary")

            if submitted:
                if not new_api_key.strip():
                    st.error("API key cannot be empty.")
                else:
                    try:
                        res = requests.post(
                            f"{API_URL}/admin/keys",
                            json={
                                "provider": new_provider,
                                "api_key": new_api_key.strip(),
                                "label": new_label.strip(),
                            },
                            timeout=5,
                        )
                        if res.status_code == 200:
                            st.success(f"Key added successfully (ID: {res.json().get('id')})")
                            st.rerun()
                        else:
                            st.error(f"Failed to add key: {res.text}")
                    except Exception as e:
                        st.error(f"Request failed: {e}")

        st.divider()

        # ---------- Existing Keys ----------
        st.markdown("### 📋 Existing Keys")
        try:
            keys_res = requests.get(f"{API_URL}/admin/keys", timeout=5)
            if keys_res.status_code == 200:
                keys_data = keys_res.json().get("keys", [])

                if not keys_data:
                    st.info("No API keys registered yet. Add one above to get started.")
                else:
                    # Summary metrics
                    total = len(keys_data)
                    enabled = sum(1 for k in keys_data if k.get("is_enabled"))
                    providers_set = set(k["provider"] for k in keys_data)
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Keys", total)
                    m2.metric("Enabled", enabled)
                    m3.metric("Providers", len(providers_set))

                    # Filter by provider
                    filter_provider = st.selectbox(
                        "Filter by provider",
                        ["All"] + sorted(providers_set),
                        key="filter_provider",
                    )
                    if filter_provider != "All":
                        keys_data = [k for k in keys_data if k["provider"] == filter_provider]

                    # Display each key as an expandable card
                    for key in keys_data:
                        kid = key["id"]
                        status_icon = "🟢" if key.get("is_enabled") else "🔴"
                        header = f"{status_icon} **{key['provider'].upper()}** — `{key.get('api_key_masked', '****')}` (ID: {kid})"
                        if key.get("label"):
                            header += f"  •  _{key['label']}_"

                        with st.expander(header, expanded=False):
                            # Rate-limit info
                            info_col1, info_col2, info_col3, info_col4 = st.columns(4)
                            info_col1.metric(
                                "Remaining Tokens",
                                key.get("rate_limit_remaining_tokens") if key.get("rate_limit_remaining_tokens") is not None else "—",
                            )
                            info_col2.metric(
                                "Remaining Requests",
                                key.get("rate_limit_remaining_requests") if key.get("rate_limit_remaining_requests") is not None else "—",
                            )
                            info_col3.metric(
                                "Token Reset",
                                key.get("rate_limit_reset_tokens") or "—",
                            )
                            info_col4.metric(
                                "Request Reset",
                                key.get("rate_limit_reset_requests") or "—",
                            )

                            st.caption(f"Last used: {key.get('last_used_at') or 'never'}  •  Created: {key.get('created_at', '—')}")

                            # Action buttons
                            act_col1, act_col2, _ = st.columns([1, 1, 3])
                            with act_col1:
                                if key.get("is_enabled"):
                                    if st.button("⏸️ Disable", key=f"disable_{kid}"):
                                        try:
                                            r = requests.patch(
                                                f"{API_URL}/admin/keys/{kid}",
                                                json={"is_enabled": False},
                                                timeout=5,
                                            )
                                            if r.status_code == 200:
                                                st.success("Key disabled.")
                                                st.rerun()
                                            else:
                                                st.error(r.text)
                                        except Exception as e:
                                            st.error(str(e))
                                else:
                                    if st.button("▶️ Enable", key=f"enable_{kid}"):
                                        try:
                                            r = requests.patch(
                                                f"{API_URL}/admin/keys/{kid}",
                                                json={"is_enabled": True},
                                                timeout=5,
                                            )
                                            if r.status_code == 200:
                                                st.success("Key enabled.")
                                                st.rerun()
                                            else:
                                                st.error(r.text)
                                        except Exception as e:
                                            st.error(str(e))
                            with act_col2:
                                if st.button("🗑️ Delete", key=f"delete_{kid}"):
                                    try:
                                        r = requests.delete(
                                            f"{API_URL}/admin/keys/{kid}",
                                            timeout=5,
                                        )
                                        if r.status_code == 200:
                                            st.success("Key deleted.")
                                            st.rerun()
                                        else:
                                            st.error(r.text)
                                    except Exception as e:
                                        st.error(str(e))
            else:
                st.error(f"Failed to fetch keys: {keys_res.text}")
        except Exception as e:
            st.error(f"Could not reach backend: {e}")
