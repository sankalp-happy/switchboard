"""
Token exhaustion test — sends the ~6K-token repo file as context
repeatedly until we hit a 429 and observe key rotation.

250K TPM / ~8K tokens per request ≈ ~30 requests to exhaust one key.
"""

import requests
import time
import json
import sys

API_URL = "http://localhost:8000"
FILE_PATH = "tests/sankalp-happy-victim-repo-8a5edab282632443.txt"

# Load the repo content
with open(FILE_PATH, "r") as f:
    repo_content = f.read()

PROMPTS = [
    "Find all SQL injection vulnerabilities in this codebase. Be detailed.",
    "Find all XSS (cross-site scripting) vulnerabilities. Show exact lines.",
    "Find all authentication and authorization flaws.",
    "Find all hardcoded secrets and credentials.",
    "Find all insecure deserialization issues.",
    "Find all path traversal vulnerabilities.",
    "Find all CSRF vulnerabilities.",
    "Find all insecure direct object reference (IDOR) issues.",
    "Find all command injection vulnerabilities.",
    "Find all sensitive data exposure issues.",
    "List every security vulnerability with OWASP Top 10 classification.",
    "Generate a penetration test report for this codebase.",
    "What are the most critical vulnerabilities that need immediate fixing?",
    "Find all missing input validation and sanitization.",
    "Analyze the database queries for injection attacks.",
    "Find all improper error handling that leaks info.",
    "Identify all missing security headers.",
    "Find all insecure file operations.",
    "Analyze for broken access control patterns.",
    "Find all race condition vulnerabilities.",
    "Do a complete SAST analysis of this code.",
    "What npm packages have known CVEs?",
    "Find all open redirect vulnerabilities.",
    "Analyze the backup file for sensitive data leaks.",
    "Find all session management flaws.",
    "Identify all missing encryption of sensitive data.",
    "Find all clickjacking vulnerabilities.",
    "Analyze all API endpoints for security issues.",
    "Find all server-side request forgery (SSRF) issues.",
    "Generate a full security audit report with severity ratings.",
    "Find all privilege escalation vectors.",
    "Identify all missing rate limiting issues.",
    "Find all unsafe regular expressions (ReDoS).",
    "Analyze for prototype pollution vulnerabilities.",
    "Find all information disclosure issues.",
    "Do a threat model for this application.",
    "Find all missing content security policies.",
    "Analyze all user input handling for injection.",
    "Find all weak cryptography usage.",
    "Generate remediation steps for every vulnerability found.",
]

def check_keys():
    """Print current key states."""
    res = requests.get(f"{API_URL}/admin/keys")
    keys = res.json()["keys"]
    for k in keys:
        print(f"  Key {k['id']} ({k['label']:15s}): enabled={k['is_enabled']}  "
              f"remaining_tokens={str(k.get('rate_limit_remaining_tokens', 'None')):>8s}  "
              f"remaining_reqs={str(k.get('rate_limit_remaining_requests', 'None')):>8s}  "
              f"last_used={k.get('last_used_at') or 'never'}")

print("=" * 70)
print("TOKEN EXHAUSTION TEST — Key Rotation Verification")
print("=" * 70)
print(f"\nRepo file: {len(repo_content)} chars (~{len(repo_content)//4} tokens)")
print(f"Prompts prepared: {len(PROMPTS)}")
print(f"\n--- Key states BEFORE test ---")
check_keys()
print()

total_tokens_used = 0
successes = 0
failures = 0

for i, prompt in enumerate(PROMPTS, 1):
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": f"You are a security auditor. Analyze this codebase:\n\n{repo_content}"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }

    start = time.time()
    try:
        res = requests.post(f"{API_URL}/v1/chat/completions", json=payload, timeout=60)
        elapsed = time.time() - start

        if res.status_code == 200:
            data = res.json()
            tokens = data.get("usage", {}).get("total_tokens", 0)
            total_tokens_used += tokens
            cache = res.headers.get("X-Cache", "?")
            provider = res.headers.get("X-Provider", "?")
            successes += 1
            print(f"  req={i:2d}  status=200  tokens={tokens:5d}  total={total_tokens_used:7d}  "
                  f"cache={cache:4s}  provider={provider}  {elapsed:.1f}s")
        else:
            failures += 1
            print(f"  req={i:2d}  status={res.status_code}  {elapsed:.1f}s  {res.text[:100]}")
    except Exception as e:
        failures += 1
        print(f"  req={i:2d}  ERROR: {e}")

    # Print key states every 10 requests
    if i % 10 == 0:
        print(f"\n--- Key states after {i} requests (total tokens: {total_tokens_used}) ---")
        check_keys()
        print()

print("\n" + "=" * 70)
print(f"RESULTS: {successes} succeeded, {failures} failed, {total_tokens_used} total tokens")
print("=" * 70)
print("\n--- Final key states ---")
check_keys()
