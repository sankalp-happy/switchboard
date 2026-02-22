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


# --- Main Area: Query Testing ---
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
                if x_cache:
                    is_cached = (x_cache == "HIT")
                else:
                    is_cached = latency < 0.2
                
                # Display Results
                st.markdown("### Response")
                st.info(content)
                
                # Display Details
                col1, col2, col3 = st.columns(3)
                col1.metric("Latency", f"{latency:.3f} s")
                
                if is_cached:
                    col2.metric("Source", "Cache ⚡")
                else:
                    col2.metric("Source", "Model 🧠")
                    
                col3.metric("Tokens used", data.get("usage", {}).get("total_tokens", 0))
                
                st.expander("Raw API Response").json(data)
                
            else:
                st.error(f"API Error ({response.status_code}): {response.text}")
                
        except Exception as e:
            st.error(f"Request failed: {str(e)}")
