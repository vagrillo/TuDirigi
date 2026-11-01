"""Operazioni media con ffmpeg/ffprobe: probe, audio, frame 1 fps 800×480."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings


class MediaError(Exception):
    pass


def _run(cmd: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise MediaError(f"binario non trovato: {cmd[0]} ({e})")
    except subprocess.TimeoutExpired:
        raise MediaError(f"timeout ffmpeg: {' '.join(cmd[:3])}…")


def probe(path: Path) -> Dict[str, Any]:
    """Metadati essenziali del file video."""
    p = _run(["ffprobe", "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(path)])
    if p.returncode != 0:
        raise MediaError(f"ffprobe fallito: {p.stderr[:300]}")
    data = json.loads(p.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        raise MediaError("nessuno stream video trovato")
    fps_raw = v.get("avg_frame_rate") or v.get("r_frame_rate") or "25/1"
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den or 1)
    except Exception:
        fps = 25.0
    return {
        "duration": float(data.get("format", {}).get("duration") or v.get("duration") or 0.0),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "has_audio": a is not None,
        "codec": v.get("codec_name"),
        "size": int(data.get("format", {}).get("size") or path.stat().st_size),
        "rotation": _rotation(v),
    }


def _rotation(stream: Dict[str, Any]) -> int:
    side = stream.get("side_data_list") or []
    for sd in side:
        if "rotation" in sd:
            return int(sd["rotation"])
    tags = stream.get("tags") or {}
    rot = tags.get("rotate")
    return int(rot) if rot else 0


def extract_audio(video: Path, out_wav: Path) -> Path:
    """Estrae l'audio in WAV mono 16 kHz (input ideale per whisper)."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    p = _run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
              "-c:a", "pcm_s16le", str(out_wav)], timeout=1200)
    if p.returncode != 0:
        raise MediaError(f"estrazione audio fallita: {p.stderr[-300:]}")
    return out_wav


def extract_frames(video: Path, out_dir: Path, fps: Optional[float] = None,
                   w: Optional[int] = None, h: Optional[int] = None,
                   progress_cb=None) -> List[Path]:
    """Estrae 1 frame al secondo ridimensionato a frame_w×frame_h.

    Nomi: f000000.jpg (secondo di riferimento). Ritorna la lista dei file.
    """
    s = settings()
    fps = fps if fps is not None else s.frame_fps
    w = w or s.frame_w
    h = h or s.frame_h
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(video),
           "-vf", f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
           "-q:v", "4", "-start_number", "0", str(out_dir / "f%06d.jpg")]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    # progresso: conta i file emessi
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise MediaError("timeout estrazione frame")
    if proc.returncode != 0:
        raise MediaError(f"estrazione frame fallita: {(proc.stderr.read() or '')[-300:]}")
    frames = sorted(out_dir.glob("f*.jpg"))
    if not frames:
        raise MediaError("nessun frame estratto")
    if progress_cb:
        progress_cb(1.0)
    return frames


def detect_scenes(video: Path, threshold: float = 0.32,
                  timeout: int = 900) -> List[float]:
    """Rileva i confini di scena con ffmpeg (select=gt(scene,t)) → lista di timestamp.

    Ritorna i tempi (secondi) dei cambi di scena, escluso lo 0 iniziale.
    """
    cmd = ["ffmpeg", "-i", str(video),
           "-vf", f"select='gt(scene,{threshold})',showinfo",
           "-an", "-f", "null", "-"]
    p = _run(cmd, timeout=timeout)
    times: List[float] = []
    for line in (p.stderr or "").splitlines():
        if "showinfo" in line and "pts_time:" in line:
            try:
                t = float(line.split("pts_time:")[1].split()[0])
                if t > 0.5:
                    times.append(round(t, 2))
            except (ValueError, IndexError):
                continue
    # dedup ravvicinate (<0.5 s)
    out: List[float] = []
    for t in times:
        if not out or t - out[-1] >= 0.5:
            out.append(t)
    return out


def make_thumbnail(video: Path, out_jpg: Path, at: float = 1.0) -> Path:
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    p = _run(["ffmpeg", "-y", "-ss", str(at), "-i", str(video), "-frames:v", "1",
              "-vf", "scale=480:-2", str(out_jpg)], timeout=120)
    if p.returncode != 0:
        # fallback primo frame
        p = _run(["ffmpeg", "-y", "-i", str(video), "-frames:v", "1", "-vf", "scale=480:-2",
                  str(out_jpg)], timeout=120)
    return out_jpg if p.returncode == 0 else out_jpg


def video_duration(path: Path) -> float:
    try:
        return float(probe(path)["duration"])
    except Exception:
        return 0.0


def has_audio_track(path: Path) -> bool:
    try:
        return bool(probe(path)["has_audio"])
    except Exception:
        return False
