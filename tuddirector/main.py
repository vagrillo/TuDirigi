"""TuDirector — server web su porta 18736 (tutti i riferimenti relativi, RP-friendly)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, llm, pipeline
from .config import (DATA_DIR, WEB_DIR, apply_runtime_overrides, public_status,
                     reload_settings, runtime_overrides_path)
from .director_tools import TOOLS
from .jobs import STORE
from . import skills as skills_mod

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tuddirector")

app = FastAPI(title="TuDirector", version=__version__, docs_url=None, redoc_url=None)

MAX_UPLOAD_MB = 2048
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


# ------------------------------------------------------------------ UI
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ------------------------------------------------------------------ health & meta
@app.get("/api/health")
def health() -> dict:
    s = apply_runtime_overrides()
    return {**public_status(), "stages_ok": True}


@app.get("/api/pipeline")
def pipeline_doc() -> dict:
    s = apply_runtime_overrides()
    return {
        "stages": [
            {"id": "probe", "desc": "ffprobe: durata, risoluzione, fps", "provider": "locale"},
            {"id": "extract", "desc": "audio WAV + frame 1 fps 800×480 (+ scene detection se abilitata)",
             "provider": "ffmpeg"},
            {"id": "stt", "desc": "trascrizione con timecode (cache su hash audio)",
             "provider": "fal.ai" if s.fal_key else "mock", "model": s.fal_whisper_model},
            {"id": "vision", "desc": "descrizione fotogrammi (batch 10, cache su hash frame)",
             "provider": "openrouter" if s.openrouter_api_key else "mock",
             "model": s.vision_model},
            {"id": "director", "desc": f"decisioni di regia per finestra "
             f"({'adattive a scene' if s.window_mode == 'scene' else 'fisse di ' + str(s.window_len) + ' s'})",
             "provider": "openrouter" if s.openrouter_api_key else "mock",
             "model": s.director_model},
            {"id": "assets", "desc": "immagini polaroid/citazioni + voci narranti (TTS)",
             "provider": "openverse/wikimedia" + ("+fal.ai" if s.fal_key else "")},
            {"id": "overlay", "desc": "SVG→PNG, polaroid, sticker", "provider": "cairosvg/Pillow"},
            {"id": "render", "desc": "render finale (slowmo reale con A/V sincronizzato)",
             "provider": "ffmpeg"},
        ],
        "tools": [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in TOOLS.items()],
    }


@app.get("/api/settings")
def get_settings() -> dict:
    s = apply_runtime_overrides()
    return {
        **public_status(),
        "editable": {
            "vision_model": s.vision_model,
            "director_model": s.director_model,
            "fal_whisper_model": s.fal_whisper_model,
            "fal_image_model": s.fal_image_model,
            "fal_tts_model": s.fal_tts_model,
            "stt_language": s.stt_language,
            "window_len": s.window_len,
            "window_mode": s.window_mode,
            "window_min": s.window_min,
            "window_max": s.window_max,
            "frame_fps": s.frame_fps,
            "vision_batch": s.vision_batch,
            "vision_concurrency": s.vision_concurrency,
            "director_concurrency": s.director_concurrency,
            "assets_policy": s.assets_policy,
            "out_fps": s.out_fps,
            "audio_offset_ms": s.audio_offset_ms,
        },
        "api_log": llm.API_LOG[-30:],
    }


@app.put("/api/settings")
def put_settings(payload: dict) -> dict:
    allowed = {"vision_model", "director_model", "fal_whisper_model", "fal_image_model",
               "fal_tts_model", "stt_language", "window_len", "window_mode", "window_min",
               "window_max", "frame_fps", "vision_batch", "vision_concurrency",
               "director_concurrency", "assets_policy", "out_fps", "audio_offset_ms"}
    data = runtime_overrides_path()
    cur = {}
    if data.exists():
        try:
            cur = json.loads(data.read_text())
        except Exception:
            cur = {}
    for k, v in (payload or {}).items():
        if k in allowed and v is not None:
            cur[k] = v
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data.write_text(json.dumps(cur, indent=1))
    reload_settings()
    apply_runtime_overrides()
    return {"ok": True}


@app.get("/api/logs")
def api_logs() -> dict:
    return {"log": llm.API_LOG[-100:]}


# ------------------------------------------------------------------ skills
@app.get("/api/skills")
def skills_list() -> dict:
    return {"skills": skills_mod.list_skills(), "tools": sorted(TOOLS.keys())}


@app.get("/api/skills/{skill_id}")
def skill_get(skill_id: str) -> dict:
    p = skills_mod.skill_path(skill_id)
    if not p.exists():
        raise HTTPException(404, "skill non trovata")
    return {"id": skill_id, "yaml": p.read_text()}


@app.post("/api/skills")
def skill_save(payload: dict) -> dict:
    sid = (payload or {}).get("id") or ""
    yaml_text = (payload or {}).get("yaml") or ""
    if not _SAFE_ID.match(sid):
        raise HTTPException(400, "id non valido")
    try:
        sk = skills_mod.save(sid, yaml_text)
    except skills_mod.SkillError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "skill": sk}


@app.delete("/api/skills/{skill_id}")
def skill_delete(skill_id: str) -> dict:
    if skill_id == "base":
        raise HTTPException(400, "la skill 'base' non si può eliminare")
    return {"ok": skills_mod.delete(skill_id)}


# ------------------------------------------------------------------ jobs
@app.post("/api/jobs")
async def create_job(files: List[UploadFile] = File(...),
                     skill: str = Form("base"), title: str = Form("")) -> dict:
    if not files:
        raise HTTPException(400, "nessun file caricato")
    job = STORE.create(title=title or files[0].filename or "Video", skill=skill or "base",
                       options={})
    d = STORE.job_dir(job["id"])
    (d / "source").mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(files):
        suffix = Path(f.filename or f"track-{i}.mp4").suffix or ".mp4"
        dest = d / "source" / f"track-{i}{suffix}"
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_MB << 20:
                    out.close()
                    shutil.rmtree(d, ignore_errors=True)
                    raise HTTPException(413, f"file oltre {MAX_UPLOAD_MB} MB")
                out.write(chunk)
        job["tracks"].append({"path": f"source/track-{i}{suffix}",
                              "name": f.filename or f"track-{i}"})
    STORE.save(job)
    threading.Thread(target=pipeline.run_job, args=(job["id"],), daemon=True).start()
    return {"ok": True, "job": job}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": [{k: j[k] for k in ("id", "title", "status", "stage", "stages",
                                        "created_at", "skill", "output") if k in j}
                     for j in STORE.list_jobs()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "job non trovato")
    return job


@app.get("/api/jobs/{job_id}/log")
def get_job_log(job_id: str, level: str = "info", limit: int = 2000) -> dict:
    if not _SAFE_ID.match(job_id):
        raise HTTPException(400, "id non valido")
    if STORE.get(job_id) is None:
        raise HTTPException(404, "job non trovato")
    from . import joblog
    return {"log": joblog.read(job_id, limit=min(5000, max(50, limit)), level=level)}


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str, payload: Optional[dict] = None) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "job non trovato")
    if job.get("status") == "running":
        return {"ok": False, "message": "già in esecuzione"}
    reset = {s: {"status": "pending", "progress": 0.0, "message": ""}
             for s in ("probe", "extract", "stt", "vision", "director", "assets",
                       "overlay", "render")}
    STORE.update(job_id, status="idle", error=None, stages=reset)
    threading.Thread(target=pipeline.run_job, args=(job_id,), daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return {"ok": STORE.cancel(job_id)}


@app.post("/api/jobs/{job_id}/window/{window_id}/redo")
def redo_window(job_id: str, window_id: str, payload: Optional[dict] = None) -> dict:
    note = (payload or {}).get("note")
    try:
        wres = pipeline.redo_window(job_id, window_id, note)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "window": wres}


@app.post("/api/jobs/{job_id}/render")
def render_only(job_id: str) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "job non trovato")
    if job.get("status") == "running":
        raise HTTPException(409, "job in esecuzione")

    def _task() -> None:
        try:
            from . import render as _r, skills as _sk
            from .overlay import prepare_overlays
            from . import assets as _a
            from . import tts as _tts
            job_dir = STORE.job_dir(job_id)
            job = STORE.get(job_id)
            sk = _sk.resolve(job.get("skill"))
            flat = [d for w in job.get("windows", []) for d in w.get("decisions", [])]
            _a.resolve_assets_for_decisions(flat, job_dir)
            _tts.resolve_tts_for_decisions(flat, job_dir)
            prepare_overlays(flat, job_dir)
            STORE.update(job_id, windows=job["windows"])  # persisti asset/_png/tts
            job["_skill_preset"] = sk.get("preset_effects") or {}
            STORE.stage_start(job_id, "render", "render manuale")
            _r.render_job(job, job_dir, progress_cb=lambda p: STORE.stage_progress(
                job_id, "render", p, "encoding"))
            render_id = str(int(time.time()))
            renders = list((STORE.get(job_id) or {}).get("renders") or [])
            renders.append({"id": render_id, "run": job.get("run_count") or 0,
                            "decisions": len(flat), "windows": len(job.get("windows", []))})
            STORE.update(job_id, output="output/final.mp4", error=None,
                         render_id=render_id, renders=renders[-10:])
            STORE.stage_done(job_id, "render", "render manuale completato")
        except Exception as e:
            log.exception("render manuale fallito")
            STORE.stage_fail(job_id, "render", str(e))

    threading.Thread(target=_task, daemon=True).start()
    return {"ok": True}


# ------------------------------------------------------------------ media files
@app.get("/media/jobs/{job_id}/{path:path}")
def media_file(job_id: str, path: str) -> FileResponse:
    if not _SAFE_ID.match(job_id):
        raise HTTPException(400, "id non valido")
    base = (STORE.job_dir(job_id)).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)) or not target.exists():
        raise HTTPException(404, "file non trovato")
    media_types = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png",
                   ".wav": "audio/wav", ".svg": "image/svg+xml", ".json": "application/json"}
    # output dinamici: mai cache del browser (un ri-render non deve mostrare il vecchio)
    nocache = ("output", "thumb")
    headers = {"Cache-Control": "no-store"} if any(p in path for p in nocache) else {}
    return FileResponse(target, media_type=media_types.get(target.suffix,
                        "application/octet-stream"), headers=headers)


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):
    log.exception("errore non gestito: %s", exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)[:300]})


def main() -> None:
    import uvicorn
    from .config import settings
    s = apply_runtime_overrides()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("TuDirector %s su http://%s:%d (mock mode: %s)", __version__, s.host, s.port,
             public_status()["mock_mode"])
    uvicorn.run(app, host=s.host, port=s.port, log_level="warning")


if __name__ == "__main__":
    main()
