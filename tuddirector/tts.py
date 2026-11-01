"""Voce narrante (TTS) via fal.ai + modalità mock (silenzio per test della catena).

Ogni decisione `narration` produce un clip WAV 44.1 kHz stereo salvato in
assets/narr-XX.wav; il render lo mixa nella traccia audio al tempo deciso.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from . import joblog
from .config import settings

log = logging.getLogger("tuddirector.tts")


class TTSError(Exception):
    pass


def _fal_tts(text: str, dest: Path, speed: float = 1.0) -> bool:
    s = settings()
    if not s.fal_key:
        return False
    try:
        r = requests.post(
            f"{s.fal_base}/{s.fal_tts_model}",
            headers={"Authorization": f"Key {s.fal_key}", "Content-Type": "application/json"},
            json={"text": text, "speed": max(0.5, min(2.0, speed))},
            timeout=180)
        if r.status_code != 200:
            log.warning("fal tts %s: %s", r.status_code, r.text[:200])
            return False
        data = r.json()
        # i modelli TTS fal restituiscono {"audio": {"url": …}} o {"audio_url": …}
        url = ((data.get("audio") or {}).get("url")) or data.get("audio_url")
        if not url:
            return False
        ar = requests.get(url, timeout=120)
        if ar.status_code != 200 or len(ar.content) < 500:
            return False
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(ar.content)
        # normalizza a wav 44.1k stereo per il mix
        p = subprocess.run(["ffmpeg", "-y", "-i", str(tmp), "-ar", "44100", "-ac", "2",
                            str(dest)], capture_output=True, timeout=120)
        tmp.unlink(missing_ok=True)
        return p.returncode == 0
    except (requests.RequestException, subprocess.SubprocessError) as e:
        log.warning("fal tts errore: %s", e)
        return False


def _mock_silence(seconds: float, dest: Path) -> bool:
    """Clip di silenzio: esercita la catena di mix nel mock mode."""
    p = subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", f"anullsrc=r=44100:cl=stereo:d={max(0.8, seconds):.2f}",
                        "-c:a", "pcm_s16le", str(dest)], capture_output=True, timeout=60)
    return p.returncode == 0


def generate_narration(text: str, dest: Path, speed: float = 1.0,
                       est_duration: float = 3.0) -> Optional[Path]:
    """Genera il clip TTS; in mock (o su errore) produce silenzio per non bloccare."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = settings()
    jid = joblog.current()
    joblog.info(jid, f"genero voce narrante: «{text[:60]}»", "tts")
    if s.fal_key:
        if _fal_tts(text, dest, speed):
            joblog.info(jid, f"voce generata con {s.fal_tts_model}", "tts")
            return dest
        joblog.warn(jid, f"TTS {s.fal_tts_model} fallito — uso silenzio", "tts")
    if not _mock_silence(est_duration, dest):
        return None
    joblog.info(jid, "clip silenzio (mock) generato", "tts")
    return dest


def clip_duration(path: Path) -> float:
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                           timeout=60)
        return float(p.stdout.strip() or 0)
    except Exception:
        return 0.0


def resolve_tts_for_decisions(decisions: List[Dict[str, Any]], job_dir: Path,
                              progress_cb=None) -> int:
    """Genera i clip per le decisioni narration; imposta d['tts_clip'] (rel path)."""
    needing = [(i, d) for i, d in enumerate(decisions) if d.get("type") == "narration"]
    done = 0
    for i, d in needing:
        p = d.get("params", {})
        text = str(p.get("text", "")).strip()[:300]
        if not text:
            continue
        fname = f"narr-{i:03d}.wav"
        dest = job_dir / "assets" / fname
        path = generate_narration(text, dest, float(p.get("speed", 1.0)),
                                  est_duration=max(1.0, float(d.get("dur", 3.0))))
        if path:
            d["tts_clip"] = f"assets/{fname}"
            d["params"]["_tts_dur"] = round(clip_duration(path), 2)
        done += 1
        if progress_cb:
            progress_cb(done / max(1, len(needing)))
    return done
