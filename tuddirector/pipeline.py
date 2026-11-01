"""Orchestratore della pipeline: esegue gli stadi di un job in un thread dedicato.

probe → extract → stt → vision → director → assets → overlay → render
Ogni stadio aggiorna lo stato su disco (job.json) ed è riavviabile.
"""
from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

from . import assets as assets_mod
from . import joblog
from . import director, llm, media, render, skills as skills_mod, stt as stt_mod, vision as vision_mod
from .config import apply_runtime_overrides, settings
from .jobs import STORE

log = logging.getLogger("tuddirector.pipeline")


def run_job(job_id: str) -> None:
    try:
        _run(job_id)
    except Exception as e:  # never crash the thread silently
        log.error("job %s errore: %s\n%s", job_id, e, traceback.format_exc())
        STORE.stage_fail(job_id, STORE.get(job_id).get("stage") or "probe", str(e))


def _check_cancel(job_id: str) -> None:
    if STORE.is_cancelled(job_id):
        raise InterruptedError("job annullato")


def _run(job_id: str) -> None:
    apply_runtime_overrides()
    s = settings()
    job = STORE.get(job_id)
    if job is None:
        return
    STORE.register_thread(job_id)
    tok = joblog.set_current(job_id)
    run_n = int(job.get("run_count") or 0) + 1
    STORE.update(job_id, run_count=run_n)
    joblog.info(job_id, f"═▶ RUN #{run_n} avviato — skill: {job.get('skill')}, "
                        f"tracce: {len(job.get('tracks', []))}", "pipeline")
    try:
        job_dir = STORE.job_dir(job_id)

        # ---------------------------------------------------------- 0. probe
        STORE.stage_start(job_id, "probe", "analisi dei file")
        tracks_meta: List[Dict[str, Any]] = []
        for i, tr in enumerate(job["tracks"]):
            _check_cancel(job_id)
            p = job_dir / tr["path"]
            meta = media.probe(p)
            meta["path"] = tr["path"]
            meta["role"] = "main" if i == 0 else f"cam-{i}"
            tracks_meta.append(meta)
        STORE.update(job_id, tracks=tracks_meta)
        # fps di uscita: 0 = auto → fedele alla sorgente (niente conversione forzata)
        out_fps = s.out_fps
        if not out_fps:
            out_fps = int(round(min(60, max(24, tracks_meta[0].get("fps") or 25))))
        STORE.update(job_id, out_fps=out_fps)
        STORE.stage_done(job_id, "probe", f"{len(tracks_meta)} tracce, out {out_fps}fps")

        # ---------------------------------------------------------- 1. extract
        STORE.stage_start(job_id, "extract", "estrazione audio e fotogrammi")
        duration = min(t["duration"] for t in tracks_meta)
        wav = job_dir / "audio" / "track-0.wav"
        has_audio = tracks_meta[0].get("has_audio")
        if has_audio:
            media.extract_audio(job_dir / tracks_meta[0]["path"], wav)
        else:
            wav = None
        STORE.stage_progress(job_id, "extract", 0.3, "audio ok")
        frames_by_track: Dict[int, list] = {}
        for ti, tm in enumerate(tracks_meta):
            _check_cancel(job_id)
            frames = media.extract_frames(
                job_dir / tm["path"], job_dir / "frames" / f"track-{ti}",
                fps=s.frame_fps if ti == 0 else s.frame_fps / 2)
            frames_by_track[ti] = [(i / (s.frame_fps if ti == 0 else s.frame_fps / 2), f)
                                   for i, f in enumerate(frames)]
            STORE.stage_progress(job_id, "extract", 0.3 + 0.6 * (ti + 1) / len(tracks_meta),
                                 f"track-{ti}: {len(frames)} frame")
        thumb = media.make_thumbnail(job_dir / tracks_meta[0]["path"], job_dir / "thumb.jpg")
        STORE.stage_done(job_id, "extract", f"{sum(len(v) for v in frames_by_track.values())} frame")

        # scene detection (per finestre adattive, se abilitata)
        scene_cuts: List[float] = []
        if s.window_mode == "scene":
            STORE.stage_progress(job_id, "extract", 1.0, "scene detection")
            try:
                scene_cuts = media.detect_scenes(job_dir / tracks_meta[0]["path"])
            except Exception as e:
                log.warning("scene detection fallita: %s", e)
            STORE.update(job_id, scene_cuts=scene_cuts)

        # ---------------------------------------------------------- 2. stt
        STORE.stage_start(job_id, "stt", "trascrizione (S2T)")
        transcript: List[Dict[str, Any]] = []
        if wav is not None:
            res = stt_mod.transcribe(wav, duration)
            transcript = res["segments"]
        STORE.update(job_id, transcript={"provider": "mock" if not wav else res["provider"],
                                         "segments": transcript})
        STORE.stage_done(job_id, "stt", f"{len(transcript)} segmenti")

        # ---------------------------------------------------------- 3. vision
        STORE.stage_start(job_id, "vision", "descrizione fotogrammi")
        captions_by_track: Dict[int, List[Dict[str, Any]]] = {}
        (job_dir / "descriptions").mkdir(parents=True, exist_ok=True)
        for ti, frames in frames_by_track.items():
            _check_cancel(job_id)
            caps = vision_mod.describe_frames(
                frames,
                progress_cb=lambda p, _ti=ti: STORE.stage_progress(
                    job_id, "vision", (_ti + p) / len(frames_by_track),
                    f"track-{_ti} in corso"),
                job_id=job_id)
            for c in caps:
                c["track"] = ti
            captions_by_track[ti] = caps
            (job_dir / "descriptions" / f"track-{ti}.json").write_text(
                __import__("json").dumps(caps, ensure_ascii=False, indent=1))
        all_captions: List[Dict[str, Any]] = []
        for ti in sorted(captions_by_track):
            all_captions.extend(captions_by_track[ti])
        STORE.update(job_id, captions=all_captions)
        STORE.stage_done(job_id, "vision", f"{len(all_captions)} didascalie")

        # ---------------------------------------------------------- 4. director
        STORE.stage_start(job_id, "director", "il regista decide")
        sk = skills_mod.resolve(job.get("skill"))
        windows_spec = director.build_windows(
            duration, mode=s.window_mode, scene_cuts=scene_cuts,
            win_min=s.window_min, win_max=s.window_max)
        windows_out: List[Dict[str, Any]] = []
        prev_summary = None
        notes = job.get("notes") or {}
        for wi, win in enumerate(windows_spec):
            _check_cancel(job_id)
            note = notes.get(f"w{wi:03d}")
            wres = director.direct_window(
                job_dir, win, transcript, all_captions, sk, prev_summary,
                duration, len(tracks_meta), user_note=note, wid=f"w{wi:03d}")
            windows_out.append(wres)
            joblog.info(job_id, f"finestra {wres['window_id']} "
                                f"({wres['t_start']:.0f}–{wres['t_end']:.0f}s): "
                                f"{len(wres.get('decisions', []))} decisioni "
                                f"[{wres.get('engine')}]", "director",
                        {"summary": wres.get("summary", ""),
                         "mood": wres.get("mood", "")})
            prev_summary = wres.get("summary")
            STORE.stage_progress(job_id, "director", (wi + 1) / len(windows_spec),
                                 f"finestra {wi + 1}/{len(windows_spec)}")
        STORE.update(job_id, windows=windows_out, skill_used=sk.get("id"))
        n_dec = sum(len(w.get("decisions", [])) for w in windows_out)
        _ = n_dec
        STORE.stage_done(job_id, "director", f"{n_dec} decisioni su {len(windows_spec)} finestre")

        # ---------------------------------------------------------- 5. assets
        STORE.stage_start(job_id, "assets", "ricerca immagini + voce narrante")
        flat = [d for w in windows_out for d in w.get("decisions", [])]
        assets_mod.resolve_assets_for_decisions(
            flat, job_dir, progress_cb=lambda p: STORE.stage_progress(job_id, "assets", p * 0.7))
        from . import tts as tts_mod
        n_tts = tts_mod.resolve_tts_for_decisions(
            flat, job_dir, progress_cb=lambda p: STORE.stage_progress(job_id, "assets",
                                                                      0.7 + p * 0.3))
        STORE.stage_done(job_id, "assets",
                         f"{len([d for d in flat if d.get('asset')])} immagini, {n_tts} voci")

        # ---------------------------------------------------------- 6. overlay
        STORE.stage_start(job_id, "overlay", "composizione overlay")
        from .overlay import prepare_overlays
        prepare_overlays(flat, job_dir)
        STORE.stage_done(job_id, "overlay", "PNG pronti")
        # persisti asset/_png nelle decisioni (il render legge da qui)
        STORE.update(job_id, windows=windows_out)

        # ---------------------------------------------------------- 7. render
        STORE.stage_start(job_id, "render", "render finale")
        joblog.info(job_id, f"render ffmpeg: {n_dec} decisioni su {len(windows_spec)} "
                            f"finestre, out {out_fps}fps", "render")
        job_for_render = dict(STORE.get(job_id) or {})
        job_for_render["_skill_preset"] = sk.get("preset_effects") or {}
        out = render.render_job(job_for_render, job_dir,
                                progress_cb=lambda p: STORE.stage_progress(
                                    job_id, "render", p, "encoding"))
        _check_cancel(job_id)
        render_id = str(int(time.time()))
        renders = list((STORE.get(job_id) or {}).get("renders") or [])
        renders.append({"id": render_id, "run": run_n,
                        "decisions": n_dec, "windows": len(windows_spec)})
        STORE.update(job_id, output="output/final.mp4", status="done", stage=None,
                     error=None, render_id=render_id, renders=renders[-10:])
        STORE.stage_done(job_id, "render", "completato")
        joblog.info(job_id, f"═✛ RUN #{run_n} COMPLETATO — output/final.mp4 "
                            f"(render_id {render_id})", "render")
        log.info("job %s completato: %s", job_id, out)
    except InterruptedError:
        STORE.update(job_id, status="cancelled")
        log.info("job %s annullato", job_id)
    finally:
        STORE.unregister_thread(job_id)
        joblog.reset_current(tok)


def redo_window(job_id: str, window_id: str, note: str | None) -> Dict[str, Any]:
    """Ri-esegue la regia su una singola finestra (con nota utente opzionale)."""
    job = STORE.get(job_id)
    if job is None:
        raise ValueError("job non trovato")
    tok = joblog.set_current(job_id)
    try:
        return _redo_window(job_id, window_id, note, job)
    finally:
        joblog.reset_current(tok)


def _redo_window(job_id: str, window_id: str, note: str | None,
                 job: Dict[str, Any]) -> Dict[str, Any]:
    job_dir = STORE.job_dir(job_id)
    s = apply_runtime_overrides()
    sk = skills_mod.resolve(job.get("skill"))
    transcript = (job.get("transcript") or {}).get("segments", [])
    captions = job.get("captions", [])
    n_tracks = len(job.get("tracks", [])) or 1
    duration = min(t["duration"] for t in job["tracks"])
    joblog.info(job_id, f"redo regia su {window_id}" +
                (f" con nota utente: «{note[:120]}»" if note else ""), "director")
    win = None
    for w in job.get("windows", []):
        if w["window_id"] == window_id:
            win = w
            break
    if win is None:
        raise ValueError("finestra non trovata")
    notes = job.get("notes") or {}
    if note is not None:
        notes[window_id] = note
        STORE.update(job_id, notes=notes)
    wres = director.direct_window(
        job_dir, (win["t_start"], win["t_end"]), transcript, captions, sk,
        None, duration, n_tracks, user_note=note, wid=window_id)
    # sostituisci la finestra mantenendo eventuali asset già pronti
    for i, w in enumerate(job["windows"]):
        if w["window_id"] == window_id:
            wres["redo"] = True
            job["windows"][i] = wres
            break
    flat = [d for w in job["windows"] for d in w.get("decisions", [])]
    assets_mod.resolve_assets_for_decisions(
        [d for d in flat if d.get("t", 0) >= win["t_start"] - 0.1], job_dir)
    from . import tts as tts_mod
    tts_mod.resolve_tts_for_decisions(
        [d for d in flat if d.get("t", 0) >= win["t_start"] - 0.1], job_dir)
    from .overlay import prepare_overlays
    prepare_overlays(flat, job_dir)
    STORE.update(job_id, windows=job["windows"])
    return wres
