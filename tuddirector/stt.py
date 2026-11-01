"""Speech-to-Text via fal.ai (whisper/wizper) + modalità mock.

Output normalizzato: [{"start": float, "end": float, "text": str}]
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from . import cache
from .config import settings

log = logging.getLogger("tuddirector.stt")


class STTError(Exception):
    pass


# ------------------------------------------------------------------ mock mode
_MOCK_LINES = [
    "Ciao a tutti e benvenuti su questo nuovo video.",
    "Oggi vi voglio raccontare una cosa incredibile che mi è successa.",
    "Guardate che scena: qui state vedendo esattamente il momento dell'arrivo.",
    "Come potete vedere la situazione era davvero critica.",
    "Ho visto un'auto cappottata proprio al centro dell'incrocio.",
    "Vi giuro, era una scena da film, uno spettacolo impressionante.",
    "E ora guardatevi questo rallenty lentissimo del momento chiave.",
    "Parlando di questo, mi viene in mente il famoso libro di Dante, la Divina Commedia.",
    "Come diceva Dante nel Inferno, lasciate ogni speranza o voi ch'entrate.",
    "Guardate questo dettaglio: è fondamentale per capire quello che dico.",
    "Sì esatto, proprio così: il dettaglio è tutto qui.",
    "Se avete domande scrivetele nei commenti qui sotto.",
    "E con questo vi saluto, grazie per aver seguito il video fino alla fine!",
]


def mock_transcribe(duration: float) -> List[Dict[str, Any]]:
    """Segmenti fittizi distribuiti sulla durata reale del video."""
    if duration <= 0:
        duration = 60.0
    segs: List[Dict[str, Any]] = []
    t = 1.0
    i = 0
    while t < duration - 1:
        text = _MOCK_LINES[i % len(_MOCK_LINES)]
        dur = min(max(3.5, len(text) * 0.075), duration - 1.0 - t)
        segs.append({"start": round(t, 2), "end": round(t + dur, 2), "text": text})
        t += dur + 0.8
        i += 1
    return segs


# ------------------------------------------------------------------ fal.ai
def _fal_call(model_path: str, payload: Dict[str, Any], timeout: int = 600) -> Dict[str, Any]:
    s = settings()
    if not s.fal_key:
        raise STTError("FAL_KEY non configurata")
    headers = {"Authorization": f"Key {s.fal_key}", "Content-Type": "application/json"}
    url = f"{s.fal_base}/{model_path}"
    t0 = time.time()
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise STTError(f"fal.ai {r.status_code}: {r.text[:300]}")
    log.info("fal %s ok in %.1fs", model_path, time.time() - t0)
    return r.json()


def fal_upload(path: Path, content_type: str = "audio/wav") -> str:
    """Carica un file sul fal Storage e ritorna l'URL pubblico.

    Gli endpoint fal richiedono input per URL: i data URI base64 non sono
    supportati (422 "Unsupported data URL"). Procedura ufficiale:
    POST /storage/upload/initiate → PUT dei byte sull'upload_url.
    """
    s = settings()
    if not s.fal_key:
        raise STTError("FAL_KEY non configurata")
    headers = {"Authorization": f"Key {s.fal_key}"}
    r = requests.post(f"{s.fal_storage_base}/storage/upload/initiate",
                      headers=headers,
                      json={"content_type": content_type, "file_name": path.name},
                      timeout=30)
    if r.status_code != 200:
        raise STTError(f"fal storage initiate {r.status_code}: {r.text[:300]}")
    data = r.json()
    upload_url, file_url = data.get("upload_url"), data.get("file_url")
    if not upload_url or not file_url:
        raise STTError(f"fal storage risposta inattesa: {list(data.keys())}")
    pr = requests.put(upload_url, data=path.read_bytes(),
                      headers={"Content-Type": content_type}, timeout=600)
    if pr.status_code not in (200, 201, 204):
        raise STTError(f"fal storage upload {pr.status_code}: {pr.text[:300]}")
    log.info("fal storage upload ok: %s (%.1f KB)", path.name, path.stat().st_size / 1024)
    return file_url


def _normalize(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    chunks = payload.get("chunks") or []
    for c in chunks:
        ts = c.get("timestamp") or [None, None]
        start = float(ts[0] or 0.0)
        end = float(ts[1] or ts[0] or 0.0)
        text = (c.get("text") or "").strip()
        if not text:
            continue
        if end <= start:
            end = start + 1.0
        segs.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    if not segs and payload.get("text"):
        segs.append({"start": 0.0, "end": 0.0, "text": payload["text"].strip()})
    return _merge_short(segs)


def _merge_short(segs: List[Dict[str, Any]], min_len: float = 0.8) -> List[Dict[str, Any]]:
    """Fonde i frammenti troppo brevi con il precedente."""
    out: List[Dict[str, Any]] = []
    for seg in segs:
        if out and (seg["end"] - seg["start"]) < min_len:
            out[-1]["end"] = seg["end"]
            out[-1]["text"] = (out[-1]["text"].rstrip(" .!?") + " " + seg["text"]).strip()
        else:
            out.append(dict(seg))
    return out


def transcribe(audio_wav: Path, duration: float, model: Optional[str] = None) -> Dict[str, Any]:
    """Ritorna {"segments": [...], "provider": "fal"|"mock", "model": str}.

    Con cache: lo stesso audio (hash del contenuto + modello) non viene ritrascritto.
    L'audio viene caricato sul fal Storage (URL pubblico) prima della chiamata.
    """
    s = settings()
    model = model or s.fal_whisper_model
    if not s.fal_key or s.stt_provider == "mock":
        return {"segments": mock_transcribe(duration), "provider": "mock", "model": "mock"}
    try:
        digest = hashlib.md5(audio_wav.read_bytes()).hexdigest()
    except Exception:
        digest = ""
    cache_key = f"{model}:{digest}"
    if digest:
        cached = cache.get("stt", cache_key)
        if cached:
            return {"segments": cached, "provider": "fal-cache", "model": model}
    audio_url = fal_upload(audio_wav, "audio/wav")
    payload: Dict[str, Any] = {
        "audio_url": audio_url,
        "task": "transcribe",
        "chunk_level": "segment",
        "version": "3",
    }
    if s.stt_language:
        payload["language"] = s.stt_language  # es. "it": trascrizione in italiano
    data = _fal_call(model, payload, timeout=max(600, int(duration) * 6))
    segments = _normalize(data)
    if digest and segments:
        cache.put("stt", cache_key, segments)
    return {"segments": segments, "provider": "fal", "model": model}


def segments_in_window(segments: List[Dict[str, Any]], t0: float, t1: float,
                       overlap: float = 0.0) -> List[Dict[str, Any]]:
    """Segmenti che toccano [t0-overlap, t1+overlap]."""
    out = []
    for seg in segments:
        if seg["end"] >= t0 - overlap and seg["start"] <= t1 + overlap:
            out.append(seg)
    return out
