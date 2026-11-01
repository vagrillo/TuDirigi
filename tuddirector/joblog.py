"""Log di esecuzione per job: storia completa in data/jobs/<id>/logs/run.jsonl.

Ogni voce: {"t", "level", "stage", "msg", "run", "data"}. I moduli che hanno a
che fare con un job chiamano log(); per i moduli profondi (llm, stt, assets) il
job "corrente" viaggia in un contextvar impostato dalla pipeline.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import DATA_DIR

_pylog = logging.getLogger("tuddirector.joblog")

_current: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tuddirector_job", default=None)

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

MAX_DATA_LEN = 400


def set_current(job_id: Optional[str]) -> contextvars.Token:
    return _current.set(job_id)


def reset_current(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> Optional[str]:
    return _current.get()


def _path(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id / "logs" / "run.jsonl"


def _lock(job_id: str) -> threading.Lock:
    with _locks_guard:
        if job_id not in _locks:
            _locks[job_id] = threading.Lock()
        return _locks[job_id]


def log(job_id: Optional[str], msg: str, level: str = "info", stage: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None) -> None:
    """Aggiunge una voce al log del job (e specchia su stdout del server)."""
    if not job_id:
        return
    entry: Dict[str, Any] = {"t": round(time.time(), 3), "level": level, "msg": str(msg)[:500]}
    if stage:
        entry["stage"] = stage
    if data:
        try:
            s = json.dumps(data, ensure_ascii=False, default=str)
            entry["data"] = json.loads(s[:MAX_DATA_LEN * 10]) if len(s) > MAX_DATA_LEN \
                else data
        except Exception:
            entry["data"] = {"raw": str(data)[:MAX_DATA_LEN]}
    p = _path(job_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock(job_id):
            with p.open("a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        _pylog.warning("joblog non scrivibile per %s: %s", job_id, e)
    # specchio su stdout
    lvl = {"info": logging.INFO, "warn": logging.WARNING,
           "error": logging.ERROR}.get(level, logging.INFO)
    _pylog.log(lvl, "[%s]%s %s", job_id, f" ({stage})" if stage else "", entry["msg"])


def info(job_id: Optional[str], msg: str, stage: Optional[str] = None,
         data: Optional[Dict[str, Any]] = None) -> None:
    log(job_id, msg, "info", stage, data)


def warn(job_id: Optional[str], msg: str, stage: Optional[str] = None,
         data: Optional[Dict[str, Any]] = None) -> None:
    log(job_id, msg, "warn", stage, data)


def error(job_id: Optional[str], msg: str, stage: Optional[str] = None,
          data: Optional[Dict[str, Any]] = None) -> None:
    log(job_id, msg, "error", stage, data)


def read(job_id: str, limit: int = 2000, level: Optional[str] = None) -> list:
    """Legge le ultime voci del log (eventualmente filtrate per livello minimo)."""
    p = _path(job_id)
    if not p.exists():
        return []
    order = {"info": 0, "warn": 1, "error": 2}
    min_l = order.get(level or "info", 0)
    out = []
    try:
        with p.open() as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if order.get(e.get("level", "info"), 0) >= min_l:
                    out.append(e)
    except OSError:
        return []
    return out[-limit:]
