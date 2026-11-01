"""Cache su disco per risultati AI costosi (vision per frame, STT per audio).

data/cache/<kind>/<md5(key)>.json — evita di pagiare due volte lo stesso lavoro
quando un job viene ri-eseguito o condividere gli stessi frame/clip.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"


def _path(kind: str, key: str) -> Path:
    h = hashlib.md5(key.encode()).hexdigest()
    return CACHE_DIR / kind / f"{h}.json"


def get(kind: str, key: str) -> Optional[Any]:
    p = _path(kind, key)
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def put(kind: str, key: str, value: Any) -> None:
    try:
        p = _path(kind, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(p)
    except Exception:
        pass
