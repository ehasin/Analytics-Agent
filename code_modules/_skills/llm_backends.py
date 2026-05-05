"""
llm_backends.py — LLM client initialization and unified call interface.

API keys are read from Colab Secrets (google.colab.userdata).
Only backends you actually use need keys configured.

Required packages per backend:
  - claude:  anthropic
  - groq:    groq
  - gemini:  google-genai   (pip install google-genai)

Provides:
  - init_backends      Set up clients for one or more providers → dict of clients
  - call_llm           Unified call with retry logic
  - MODEL_MAP  {provider: {tier: model_name}}  (tier 0=fast, 1=default, 2=reasoning)
"""

import os
import time

# ── Model registry ───────────────────────────────────────────

MODEL_MAP: dict[str, dict[int, str]] = {
    "claude": {
        0: "claude-haiku-4-5",
        1: "claude-sonnet-4-6",
        2: "claude-opus-4-7",
    },
    "groq": {
        0: "llama-3.1-8b-instant",
        1: "llama-3.3-70b-versatile",
        2: "openai/gpt-oss-120b",
    },
    "gemini": {
        0: "gemini-2.5-flash-lite",
        1: "gemini-2.5-flash",
        2: "gemini-2.5-pro",
    },
}


# ── Client initialization ───────────────────────────────────

def _get_key(secret_name: str) -> str:
    """Read a key from Colab Secrets, falling back to env vars."""
    try:
        from google.colab import userdata
        return userdata.get(secret_name)
    except (ImportError, Exception):
        val = os.environ.get(secret_name)
        if not val:
            raise RuntimeError(
                f"Key '{secret_name}' not found in Colab Secrets or env vars"
            )
        return val


def init_backends(
    backends: list[str],
    secret_names: dict[str, str],
    test: bool = True,
    api_keys: dict[str, str] | None = None,
) -> dict:
    """Initialize LLM clients for the requested backends.

    Args:
        backends:     e.g. ["groq", "claude"]
        secret_names: backend → Colab Secret / env-var name. Used when
                      api_keys does not supply the key (Colab / CLI path).
        test:         send a quick ping to each backend on init
        api_keys:     optional {backend: key_string}. Takes precedence over
                      secret_names lookup. Used by Streamlit so user keys
                      are never written to process env vars.

    Returns:
        dict with:
          - "clients": {backend_name: client_obj}
          - "active":  [backend_name, ...]
          - "pings":   {backend_name: ping_response_str} if test=True, else {}
    """
    clients: dict = {}
    pings: dict = {}

    for name in backends:
        # Prefer explicitly-passed key (Streamlit) over secret lookup (Colab/CLI)
        if api_keys and name in api_keys:
            key = api_keys[name]
        else:
            if name not in secret_names:
                raise ValueError(f"No secret name configured for backend '{name}'")
            key = _get_key(secret_names[name])

        if name == "claude":
            from anthropic import Anthropic
            client = Anthropic(api_key=key)
            if test:
                r = client.messages.create(
                    model=MODEL_MAP["claude"][1], max_tokens=32, timeout=30,
                    messages=[{"role": "user", "content": "Say hi in 3 words"}],
                )
                pings[name] = r.content[0].text.strip()

        elif name == "groq":
            from groq import Groq
            client = Groq(api_key=key, max_retries=0)  # we handle retries ourselves
            if test:
                r = client.chat.completions.create(
                    model=MODEL_MAP["groq"][1], max_tokens=32,
                    messages=[{"role": "user", "content": "Say hi in 3 words"}],
                )
                pings[name] = r.choices[0].message.content.strip()

        elif name == "gemini":
            from google import genai
            client = genai.Client(api_key=key)
            if test:
                r = client.models.generate_content(
                    model=MODEL_MAP["gemini"][1], contents="Say hi in 3 words",
                )
                pings[name] = r.text.strip()

        else:
            raise ValueError(f"Unknown backend: {name}")

        clients[name] = client

    return {"clients": clients, "active": list(clients.keys()), "pings": pings}


# ── Unified LLM call ─────────────────────────────────────────

def call_llm(
    prompt: str,
    backend: str,
    clients: dict,
    model: int = 1,
    max_tokens: int = 4096,
    max_retries: int = 5,
) -> str:
    """Send a prompt to the specified backend and return the text response.

    Args:
        prompt:      the user message
        backend:     "claude" | "groq" | "gemini"
        clients:     dict of provider → client (from init_backends)
        model: 0 = fast/cheap (Haiku-class), 1 = default, 2 = strong reasoning
        max_tokens:  response length cap
        max_retries: retry on 429/529 with exponential backoff
    """
    client = clients[backend]
    model_name = MODEL_MAP[backend][model]

    for attempt in range(max_retries):
        try:
            if backend == "claude":
                resp = clients["claude"].messages.create(
                    model=MODEL_MAP["claude"][model],
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=180,  # 3 min ceiling per call — prevents indefinite hangs
                )
                return resp.content[0].text

            elif backend == "groq":
                r = client.chat.completions.create(
                    model=model_name, max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return r.choices[0].message.content.strip()

            elif backend == "gemini":
                from google.genai import types
                r = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        max_output_tokens=max_tokens,
                    ),
                )
                return r.text.strip()

        except Exception as e:
            err_str = str(e)
            # Token/quota exhaustion — never retry, surface immediately
            fatal = any(code in err_str for code in [
                "413", "rate_limit_exceeded", "tokens", "TPM", "RPM",
                "too large", "reduce your message", "quota",
            ])
            if fatal:
                raise
            # Transient server errors — retry with backoff
            retryable = any(code in err_str for code in ["429", "529"])
            if retryable and attempt < max_retries - 1:
                wait = 15 * (2 ** attempt)
                print(f"    Rate limited — retrying in {wait}s (attempt {attempt + 2}/{max_retries})")
                time.sleep(wait)
            else:
                raise
            
    raise RuntimeError(f"call_llm exhausted {max_retries} retries on {backend}")
