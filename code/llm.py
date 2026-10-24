"""Minimal Gemini client: strict-JSON calls with retry/backoff, a disk cache, and token accounting.

The cache makes full runs reproducible and offline: every prompt+payload hash maps to the validated
response. Delete code/cache/llm_cache.json to force live calls (needs GEMINI_API_KEY).
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# free-tier daily quotas are per model: on a quota 429 the call moves to the next model
FALLBACK_MODELS = [MODEL, "gemini-3.5-flash", "gemini-flash-latest", "gemini-2.5-pro"]
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "llm_cache.json")
# USD per 1M tokens (gemini-2.5-flash list price); thinking tokens bill as output
PRICE_IN, PRICE_OUT = 0.30, 2.50

USAGE = {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0, "by_purpose": {}}
_exhausted = set()
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    json.dump(_cache, open(tmp, "w", encoding="utf-8"), indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


def _account(purpose, inp, out, cached, model=MODEL):
    p = USAGE["by_purpose"].setdefault((model, purpose), {"calls": 0, "cache_hits": 0, "input_tokens": 0,
                                                 "output_tokens": 0})
    p["calls"] += 1
    p["input_tokens"] += inp
    p["output_tokens"] += out
    USAGE["calls"] += 1
    USAGE["input_tokens"] += inp
    USAGE["output_tokens"] += out
    if cached:
        USAGE["cache_hits"] += 1
        p["cache_hits"] += 1


def _key(model, prompt, schema, attachment):
    key_src = json.dumps([model, prompt, schema], sort_keys=True)
    if attachment is not None:
        key_src += hashlib.sha256(attachment).hexdigest()
    return hashlib.sha256(key_src.encode()).hexdigest()


def generate_json(purpose, prompt, schema, validate, attachment=None, mime=None, retries=4):
    """Return a validated JSON object, or raise RuntimeError when every model fails.

    validate(obj) must raise ValueError on a malformed/incomplete answer; the call is then retried.
    Cached entries replay their original token counts so the usage report reflects the real run cost.
    """
    cache = _load()
    for model in FALLBACK_MODELS:
        hit = cache.get(_key(model, prompt, schema, attachment))
        if hit:
            _account(purpose, hit["usage"]["input"], hit["usage"]["output"], True, model)
            return hit["response"]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set and no cached response for " + purpose)
    parts = [{"text": prompt}]
    if attachment is not None:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(attachment).decode()}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseSchema": schema}}
    last = None
    for model in FALLBACK_MODELS:
        if model in _exhausted:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, json.dumps(body).encode(),
                                             {"Content-Type": "application/json", "x-goog-api-key": api_key})
                resp = json.load(urllib.request.urlopen(req, timeout=180))
                meta = resp.get("usageMetadata", {})
                inp = meta.get("promptTokenCount", 0)
                out = meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0)
                text = resp["candidates"][0]["content"]["parts"][0]["text"]
                obj = json.loads(text)
                validate(obj)
                _account(purpose, inp, out, False, model)
                cache[_key(model, prompt, schema, attachment)] = {
                    "purpose": purpose, "model": model, "response": obj, "usage": {"input": inp, "output": out}}
                _save()
                return obj
            except urllib.error.HTTPError as e:
                last = e
                msg = e.read().decode(errors="replace")
                if e.code == 429 and "PerDay" in msg or e.code == 404:
                    _exhausted.add(model)  # daily quota gone / model not served: next model
                    break
                time.sleep(20 * (attempt + 1) if e.code == 429 else 2 ** attempt)
            except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
                last = e
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{purpose}: no valid model response from {FALLBACK_MODELS}: {last}")


def cost(inp, out):
    return inp / 1e6 * PRICE_IN + out / 1e6 * PRICE_OUT
