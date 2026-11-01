"""Client OpenRouter con structured output (json_schema strict), retry e log costi."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import settings

log = logging.getLogger("tuddirector.llm")

API_LOG: List[Dict[str, Any]] = []   # ultime chiamate (per la UI Impostazioni/Diagnostica)


class LLMError(Exception):
    pass


def _api_log_dir() -> Path:
    from .config import DATA_DIR
    return DATA_DIR / "api_logs"


def _record(entry: Dict[str, Any]) -> None:
    API_LOG.append(entry)
    del API_LOG[:-500]  # tiene le ultime 500
    try:
        _api_log_dir().mkdir(parents=True, exist_ok=True)
        with (_api_log_dir() / "api.log").open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # log per-job (se il job corrente è impostato dalla pipeline)
    from . import joblog
    jid = joblog.current()
    if jid:
        kind = entry.get("kind", "chat")
        model = entry.get("model", "?")
        tok = f", tok p:{entry.get('prompt_tokens') or '–'} c:{entry.get('completion_tokens') or '–'}"
        if entry.get("ok"):
            joblog.info(jid, f"LLM {kind} ok — {model}{tok}, {entry.get('latency', 0)}s",
                        "llm", {k: v for k, v in entry.items() if k not in ("t",)})
        else:
            joblog.error(jid, f"LLM {kind} ERRORE — {model}: {entry.get('error', '')[:300]}",
                         "llm")


def available() -> bool:
    return bool(settings().openrouter_api_key)


def chat(messages: List[Dict[str, Any]], model: Optional[str] = None,
         json_schema: Optional[Dict[str, Any]] = None, temperature: float = 0.4,
         max_tokens: int = 4000, retries: int = 2, timeout: int = 180) -> Dict[str, Any]:
    """Chiamata chat/completions. Se json_schema è passato usa response_format strict.

    Ritorna {"content": str|None, "parsed": dict|None, "usage": {...}, "model": str}.
    """
    s = settings()
    if not s.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY non configurata")
    model = model or s.director_model
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": json_schema.get("name", "response"),
                            "strict": json_schema.get("strict", True),
                            "schema": json_schema["schema"]},
        }
    headers = {
        "Authorization": f"Bearer {s.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tuddirector.local",
        "X-Title": "TuDirector",
    }
    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = requests.post(f"{s.openrouter_base}/chat/completions", headers=headers,
                              json=body, timeout=timeout)
            latency = time.time() - t0
            if r.status_code == 429:
                last_err = f"429 rate limit: {r.text[:200]}"
                time.sleep(3 + attempt * 4)
                continue
            if r.status_code >= 400:
                _record({"t": time.time(), "model": model, "latency": round(time.time() - t0, 2),
                         "kind": "structured" if json_schema else "chat", "ok": False,
                         "error": f"{r.status_code}: {r.text[:200]}"})
                raise LLMError(f"OpenRouter {r.status_code}: {r.text[:300]}")
            data = r.json()
            msg = (data.get("choices") or [{}])[0].get("message", {})
            content = msg.get("content")
            parsed = None
            if content and json_schema is not None:
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    m = content.find("{")
                    if m >= 0:
                        try:
                            parsed = json.loads(content[m:content.rfind("}") + 1])
                        except Exception:
                            parsed = None
            usage = data.get("usage", {}) or {}
            entry = {"t": time.time(), "model": model, "latency": round(latency, 2),
                     "prompt_tokens": usage.get("prompt_tokens"),
                     "completion_tokens": usage.get("completion_tokens"),
                     "cost": usage.get("cost"), "kind": "structured" if json_schema else "chat",
                     "ok": True}
            _record(entry)
            return {"content": content, "parsed": parsed, "usage": usage, "model": model}
        except LLMError:
            raise
        except requests.RequestException as e:
            last_err = str(e)[:300]
            time.sleep(2 + attempt * 3)
    _record({"t": time.time(), "model": model, "latency": None,
             "kind": "structured" if json_schema else "chat", "ok": False,
             "error": f"irraggiungibile dopo {retries + 1} tentativi: {last_err}"})
    raise LLMError(f"OpenRouter non raggiungibile: {last_err}")
