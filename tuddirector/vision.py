"""img2txt: descrizione fotogrammi via OpenRouter vision, in batch, + mock.

Output per traccia: [{"t": float, "caption": str, "shot": str, "faces": int, "motion": str}]
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cache, llm
from .config import PROMPTS_DIR, settings

log = logging.getLogger("tuddirector.vision")

_VALID_SHOTS = {"wide", "medium", "close", "macro", "other"}


def vision_system_prompt() -> str:
    p = PROMPTS_DIR / "vision.md"
    if p.exists():
        return p.read_text()
    return "Descrivi brevemente i fotogrammi ricevuti, come un assistente di regia."


def _schema(batch_len: int) -> Dict[str, Any]:
    idx = {"type": "integer", "minimum": 0, "maximum": batch_len - 1}
    return {
        "name": "frame_batch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "frames": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "i": idx,
                            "caption": {"type": "string"},
                            "shot": {"type": "string",
                                     "enum": ["wide", "medium", "close", "macro", "other"]},
                            "faces": {"type": "integer"},
                            "motion": {"type": "string"},
                            "subject_pos": {"type": "string",
                                            "enum": ["left", "center", "right", "absent"]},
                        },
                        "required": ["i", "caption", "shot", "faces", "motion", "subject_pos"],
                    },
                }
            },
            "required": ["frames"],
        },
    }


def _user_content(batch: List[Tuple[float, Path]]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"Ricevi {len(batch)} fotogrammi consecutivi di uno stesso video, "
            "1 al secondo, numerati da 0. Descrivi ciascuno con la struttura JSON richiesta. "
            "Le didascalie devono essere concrete e utili alla regia."
        ),
    }]
    for i, (_, path) in enumerate(batch):
        b64 = base64.b64encode(path.read_bytes()).decode()
        parts.append({"type": "text", "text": f"fotogramma {i}:"})
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return parts


def _mock_caption(idx_in_batch: int, path: Path) -> Dict[str, Any]:
    """Didascalia euristica: luminosità/colore dominante + varietà narrativa."""
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB").resize((32, 20))
        px = list(img.getdata())
        mean = tuple(sum(c[i] for c in px) / len(px) for i in range(3))
        var = sum(sum(abs(c[i] - mean[i]) for i in range(3)) for c in px) / len(px)
        bright = sum(mean) / 3
    except Exception:
        bright, var = 128, 30
    scenes = [
        "una persona parla rivolta alla camera, inquadratura media",
        "panoramica su un paesaggio ampio e luminoso",
        "primo piano di un oggetto sul tavolo",
        "scena in movimento, la camera segue un soggetto",
        "interno con luce calda, soggetto parzialmente in ombra",
        "inquadratura larga di uno spazio aperto",
    ]
    shot = "close" if var > 60 else ("medium" if bright < 150 else "wide")
    pos = ["center", "absent", "right", "left", "absent", "absent"][idx_in_batch % 6]
    return {"caption": scenes[idx_in_batch % len(scenes)],
            "shot": shot, "faces": 1 if var > 40 else 0,
            "motion": "statico" if var < 30 else "movimento",
            "subject_pos": pos}


def describe_batch(batch: List[Tuple[float, Path]], model: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ritorna una didascalia per ogni frame del batch (stesso ordine).

    I frame già in cache (hash del contenuto + modello) non vengono richiesti al modello.
    """
    s = settings()
    model = model or s.vision_model
    results: List[Dict[str, Any]] = [None] * len(batch)  # type: ignore[list-item]
    to_ask: List[int] = []
    for i, (t, path) in enumerate(batch):
        try:
            digest = hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            digest = ""
        cached = cache.get("vision", f"{model}:v2:{digest}") if digest else None
        if cached:
            results[i] = {"t": t, **cached}
        else:
            to_ask.append(i)
            results[i] = {"t": t, "_digest": digest}
    if not llm.available():
        for i in (to_ask or range(len(batch))):
            t, path = batch[i]
            m = _mock_caption(i, path)
            results[i] = {"t": t, **m}
        return results
    if not to_ask:
        return results
    sub_batch = [batch[i] for i in to_ask]
    try:
        resp = llm.chat(
            messages=[{"role": "system", "content": vision_system_prompt()},
                      {"role": "user", "content": _user_content(sub_batch)}],
            model=model, json_schema=_schema(len(sub_batch)), temperature=0.2, max_tokens=1800,
        )
        frames = (resp.get("parsed") or {}).get("frames") or []
        seen = set()
        for f in frames:
            i = f.get("i")
            if isinstance(i, int) and 0 <= i < len(sub_batch) and i not in seen:
                seen.add(i)
                gi = to_ask[i]
                t, path = batch[gi]
                shot = str(f.get("shot", "medium")).lower()
                spos = str(f.get("subject_pos", "absent")).lower()
                payload = {
                    "caption": str(f.get("caption", "")).strip()[:400],
                    "shot": shot if shot in _VALID_SHOTS else "medium",
                    "faces": max(0, int(f.get("faces") or 0)),
                    "motion": str(f.get("motion", ""))[:60],
                    "subject_pos": spos if spos in ("left", "center", "right", "absent")
                                   else "absent",
                }
                results[gi] = {"t": t, **payload}
                digest = results[gi].get("_digest")
                if digest:
                    cache.put("vision", f"{model}:v2:{digest}", payload)
        for gi in to_ask:
            if not results[gi].get("caption"):
                t, path = batch[gi]
                m = _mock_caption(to_ask.index(gi), path)
                results[gi] = {"t": t, **m}
    except Exception as e:
        log.warning("batch vision fallito (%s), fallback mock", e)
        for k, gi in enumerate(to_ask):
            t, path = batch[gi]
            m = _mock_caption(k, path)
            results[gi] = {"t": t, **m}
    return [{k: v for k, v in r.items() if k != "_digest"} for r in results]


def describe_frames(frames: List[Tuple[float, Path]],
                    progress_cb: Optional[Callable[[float], None]] = None,
                    model: Optional[str] = None,
                    batch_size: Optional[int] = None,
                    concurrency: Optional[int] = None) -> List[Dict[str, Any]]:
    """Descrive tutti i frame: batching + concorrenza regolabile."""
    s = settings()
    batch_size = batch_size or s.vision_batch
    concurrency = max(1, concurrency or s.vision_concurrency)
    batches = [frames[i:i + batch_size] for i in range(0, len(frames), batch_size)]
    out: List[Dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(describe_batch, b, model): b for b in batches}
        for fut in as_completed(futs):
            try:
                out.extend(fut.result())
            except Exception as e:  # non deve mai bloccare la pipeline
                log.error("batch errore: %s", e)
            done += 1
            if progress_cb:
                progress_cb(done / max(1, len(batches)))
    out.sort(key=lambda r: r["t"])
    return out


def captions_in_window(captions: List[Dict[str, Any]], t0: float, t1: float,
                       overlap: float = 0.0, sample_every: int = 2) -> List[Dict[str, Any]]:
    """Didascalie nella finestra, campionate per contenere la dimensione del prompt."""
    sel = [c for c in captions if t0 - overlap <= c["t"] <= t1 + overlap]
    return sel[::sample_every] if sample_every > 1 else sel
