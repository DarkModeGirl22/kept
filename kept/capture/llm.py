"""Optional local AI helper through Ollama (free, runs on your own computer, no key).

The app never depends on it. Every call has a time limit, must return strict JSON, and
its answer is checked by code afterwards. If Ollama is off, slow, or answers badly, the
caller falls back to plain rules.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("KEPT_OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("KEPT_MODEL", "qwen2.5:3b")
TIMEOUT = float(os.environ.get("KEPT_AI_TIMEOUT", "20"))

_status_cache = {"t": 0.0, "ok": False, "models": []}
_enabled = {"on": os.environ.get("KEPT_USE_AI", "1") != "0"}


def set_enabled(on: bool):
    _enabled["on"] = bool(on)


def is_enabled() -> bool:
    return _enabled["on"]


def status(force: bool = False) -> dict:
    """Is Ollama running, and is the model downloaded? Cached for 30 seconds."""
    if not force and time.time() - _status_cache["t"] < 30:
        return dict(_status_cache)
    ok, models = False, []
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5) as r:
            data = json.loads(r.read().decode())
            models = [m.get("name", "") for m in data.get("models", [])]
            ok = True
    except (urllib.error.URLError, OSError, ValueError):
        ok = False
    _status_cache.update(t=time.time(), ok=ok, models=models)
    return dict(_status_cache)


def available() -> bool:
    if not _enabled["on"]:
        return False
    s = status()
    return s["ok"] and any(m == MODEL or m.startswith(MODEL.split(":")[0]) for m in s["models"])


def chat_json(system: str, user: str, timeout: float | None = None) -> dict | None:
    """Ask the local model for a JSON object. Returns None on any problem."""
    if not available():
        return None
    body = json.dumps({
        "model": MODEL,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            data = json.loads(r.read().decode())
        content = data.get("message", {}).get("content", "")
        out = json.loads(content)
        return out if isinstance(out, dict) else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
