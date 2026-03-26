"""
core/llm.py
ARIA™ — LLM wrapper
Auto-switches between Groq (Railway) and LM Studio (local).
OUP International Ltd, 2026
"""

import os
import logging
import httpx
from groq import Groq

logger = logging.getLogger("aria.llm")

_groq_client: Groq = None


def _get_groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


def call_llm(
    prompt: str,
    system: str = "You are a helpful AI agent in the ARIA marketing system.",
    max_tokens: int = 1000,
    temperature: float = 0.7,
    json_mode: bool = False
) -> str:
    """
    Call LLM. Uses Groq on Railway, falls back to LM Studio locally.
    Returns raw string response.
    """
    use_groq = bool(os.environ.get("GROQ_API_KEY"))

    if use_groq:
        return _call_groq(prompt, system, max_tokens, temperature, json_mode)
    else:
        return _call_local(prompt, system, max_tokens, temperature)


def _call_groq(prompt, system, max_tokens, temperature, json_mode) -> str:
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        client = _get_groq()
        res = client.chat.completions.create(**kwargs)
        return res.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Groq call failed: {e}")
        raise


def _call_local(prompt, system, max_tokens, temperature) -> str:
    """Call LM Studio at localhost:1234."""
    url = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
    model = os.environ.get("LM_STUDIO_MODEL", "local-model")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        with httpx.Client(timeout=30) as client:
            res = client.post(url, json=payload)
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        logger.error(f"Local LLM call failed: {e}")
        raise


def score_with_llm(prompt: str, context: str = "") -> dict:
    """
    Convenience: call LLM expecting a JSON score response.
    Returns parsed dict or raises.
    """
    import json
    full_prompt = f"{context}\n\n{prompt}" if context else prompt
    raw = call_llm(full_prompt, max_tokens=400, temperature=0.3)
    clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(clean)
