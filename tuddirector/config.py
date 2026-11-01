"""Configurazione runtime di TuDirector.

Legge le variabili da ambiente / file .env nella root del progetto.
Le chiavi API NON vengono scritte nel repo: solo .env locale.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SKILLS_DIR = Path(os.environ.get("TUDDIRECTOR_SKILLS_DIR", ROOT / "tuddirector" / "skills"))
PROMPTS_DIR = Path(os.environ.get("TUDDIRECTOR_PROMPTS_DIR", ROOT / "tuddirector" / "prompts"))
WEB_DIR = ROOT / "web"


class Settings(BaseSettings):
    """Impostazioni globali (override via ambiente con prefisso TD_)."""

    # --- server -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 18736
    max_upload_mb: int = 2048

    # --- provider AI --------------------------------------------------------
    openrouter_api_key: Optional[str] = None
    openrouter_base: str = "https://openrouter.ai/api/v1"
    fal_key: Optional[str] = None
    fal_base: str = "https://fal.run"
    fal_storage_base: str = "https://rest.alpha.fal.ai"

    # --- modelli ------------------------------------------------------------
    vision_model: str = "google/gemini-2.5-flash"
    director_model: str = "anthropic/claude-sonnet-4.5"
    stt_provider: str = "fal"  # fal | mock
    fal_whisper_model: str = "fal-ai/whisper"
    fal_image_model: str = "fal-ai/flux/schnell"
    fal_tts_model: str = "fal-ai/minimax/speech-02-turbo"

    # --- pipeline -----------------------------------------------------------
    stt_language: str = "it"          # lingua del parlato (ISO-639-1: it, en, es, fr…)
    frame_fps: float = 1.0            # fotogrammi al secondo per l'analisi
    frame_w: int = 800
    frame_h: int = 480
    window_len: float = 30.0          # durata finestra regista (s)
    window_mode: str = "fixed"        # fixed | scene (finestre adattive alla scene detection)
    window_min: float = 18.0          # durata minima finestra (modalità scene)
    window_max: float = 48.0          # durata massima finestra (modalità scene)
    window_overlap: float = 5.0       # overlap contestuale mostrato al LLM (s)
    vision_batch: int = 10            # frame per richiesta vision
    vision_concurrency: int = 3
    director_concurrency: int = 2
    assets_policy: str = "auto"       # auto: openverse → wikimedia → fal generate
    out_w: int = 1280
    out_h: int = 720
    out_fps: int = 0                  # 0 = auto: usa gli fps della sorgente (clamp 24–60)
    audio_offset_ms: int = 0          # offset audio nel render: >0 = voce più tardi,
                                      # <0 = voce più presto (compensa desync sorgente)

    class Config:
        env_prefix = "TD_"
        env_file = str(ROOT / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings


def runtime_overrides_path() -> Path:
    return DATA_DIR / "runtime_overrides.json"


def apply_runtime_overrides() -> Settings:
    """Applica gli override salvati dalla UI Impostazioni (modelli, parametri)."""
    s = settings()
    p = runtime_overrides_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            for k, v in data.items():
                if hasattr(s, k) and v is not None:
                    setattr(s, k, v)
        except Exception:
            pass
    return s


def public_status() -> dict:
    """Stato sicuro da esporre via API (mai le chiavi)."""
    s = apply_runtime_overrides()
    return {
        "version": __import__("tuddirector").__version__,
        "openrouter_configured": bool(s.openrouter_api_key),
        "fal_configured": bool(s.fal_key),
        "models": {
            "vision": s.vision_model,
            "director": s.director_model,
            "stt": s.fal_whisper_model,
            "image_gen": s.fal_image_model,
        },
        "mock_mode": not (s.openrouter_api_key and s.fal_key),
        "pipeline": {
            "frame_fps": s.frame_fps,
            "frame_size": [s.frame_w, s.frame_h],
            "window_len": s.window_len,
            "out_size": [s.out_w, s.out_h],
        },
    }
