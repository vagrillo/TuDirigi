"""Gestione job: stato su disco + avanzamento pipeline.

Ogni job vive in data/jobs/<id>/ con job.json come fonte di verità.
La pipeline gira in un thread dedicato (ffmpeg via subprocess, API via requests/httpx).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

STAGES = ["probe", "extract", "stt", "vision", "director", "assets", "overlay", "render"]

JOBS_DIR = DATA_DIR / "jobs"


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class JobStore:
    """Registro job con accesso thread-safe allo stato su disco."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running: Dict[str, threading.Event] = {}  # job_id -> cancel event

    # ------------------------------------------------------------- percorsi
    @staticmethod
    def job_dir(job_id: str) -> Path:
        return JOBS_DIR / job_id

    @staticmethod
    def job_path(job_id: str) -> Path:
        return JOBS_DIR / job_id / "job.json"

    # ------------------------------------------------------------- CRUD
    def list_jobs(self) -> List[Dict[str, Any]]:
        out = []
        if JOBS_DIR.exists():
            for d in sorted(JOBS_DIR.iterdir(), reverse=True):
                p = d / "job.json"
                if p.exists():
                    try:
                        out.append(json.loads(p.read_text()))
                    except Exception:
                        continue
        return out

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        p = self.job_path(job_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    def save(self, job: Dict[str, Any]) -> None:
        d = self.job_dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = self.job_path(job["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1))
        tmp.replace(self.job_path(job["id"]))

    def create(self, title: str, skill: str, options: Dict[str, Any]) -> Dict[str, Any]:
        job: Dict[str, Any] = {
            "id": new_id(),
            "created_at": time.time(),
            "title": title or "Video senza titolo",
            "skill": skill,
            "options": options or {},
            "status": "idle",       # idle | running | done | error | cancelled
            "stage": None,
            "stages": {s: {"status": "pending", "progress": 0.0, "message": ""} for s in STAGES},
            "tracks": [],           # metadati ffprobe per traccia
            "windows": [],          # finestre regista (output director)
            "notes": {},            # note utente per finestra (redo)
            "error": None,
            "output": None,
        }
        self.save(job)
        return job

    def update(self, job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            job.update(fields)
            self.save(job)
            return job

    def stage_start(self, job_id: str, stage: str, message: str = "") -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            job["stage"] = stage
            st = job["stages"].get(stage, {"status": "pending", "progress": 0.0, "message": ""})
            st.update({"status": "running", "progress": 0.0, "message": message, "t_start": time.time()})
            job["stages"][stage] = st
            job["status"] = "running"
            self.save(job)

    def stage_progress(self, job_id: str, stage: str, progress: float, message: str = "") -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            st = job["stages"].get(stage)
            if st is None:
                return
            st["progress"] = max(st.get("progress", 0.0), min(1.0, progress))
            if message:
                st["message"] = message
            job["stages"][stage] = st
            self.save(job)

    def stage_done(self, job_id: str, stage: str, message: str = "") -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            st = job["stages"].get(stage, {})
            st.update({"status": "done", "progress": 1.0, "t_end": time.time(),
                       "message": message or st.get("message", "")})
            job["stages"][stage] = st
            self.save(job)

    def stage_fail(self, job_id: str, stage: str, error: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            st = job["stages"].get(stage, {})
            st.update({"status": "error", "t_end": time.time(), "message": error[:500]})
            job["stages"][stage] = st
            job["status"] = "error"
            job["error"] = f"{stage}: {error[:500]}"
            self.save(job)

    # ------------------------------------------------------------- ciclo di vita
    def is_cancelled(self, job_id: str) -> bool:
        ev = self._running.get(job_id)
        return bool(ev and ev.is_set())

    def cancel(self, job_id: str) -> bool:
        ev = self._running.get(job_id)
        if ev:
            ev.set()
        job = self.get(job_id)
        if job and job.get("status") in ("idle", "running"):
            self.update(job_id, status="cancelled")
            return True
        return False

    def register_thread(self, job_id: str) -> threading.Event:
        ev = threading.Event()
        self._running[job_id] = ev
        return ev

    def unregister_thread(self, job_id: str) -> None:
        self._running.pop(job_id, None)


STORE = JobStore()
