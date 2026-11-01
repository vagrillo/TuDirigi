"""Il Regista: finestre 30 s → decisioni JSON structured output.

Composizione prompt: base (prompts/director.md) + skill attiva + registry tool +
contesto tecnico + dati della finestra (trascrizione + didascalie + summary precedente).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from . import llm
from .config import PROMPTS_DIR, settings
from .director_tools import (director_json_schema, registry_for_prompt, validate_decision)
from .stt import segments_in_window
from .vision import captions_in_window

log = logging.getLogger("tuddirector.director")

MOODS = ["neutro", "giocoso", "drammatico", "tensione", "info", "epico"]


# ---------------------------------------------------------------- finestre
def build_windows(duration: float, window_len: Optional[float] = None,
                  mode: str = "fixed", scene_cuts: Optional[List[float]] = None,
                  win_min: float = 18.0, win_max: float = 48.0) -> List[Tuple[float, float]]:
    """Costruisce le finestre di regia.

    - fixed: finestre regolari di window_len secondi (default, come da specifica).
    - scene: finestre adattive — il taglio preferisce un confine di scena dentro la
      tolleranza [win_min, win_max], così la finestra contiene scene intere.
    """
    s = settings()
    L = window_len or s.window_len
    if mode == "scene" and scene_cuts:
        if duration <= max(1.0, win_min):
            return [(0.0, max(1.0, duration))]
        return _windows_scene(duration, scene_cuts, L, win_min, win_max)
    if duration <= L:
        return [(0.0, max(1.0, duration))]
    wins = []
    t0 = 0.0
    while t0 < duration - 0.5:
        wins.append((round(t0, 2), round(min(t0 + L, duration), 2)))
        t0 += L
    return wins


def _windows_scene(duration: float, cuts: List[float], target: float,
                   win_min: float, win_max: float) -> List[Tuple[float, float]]:
    wins: List[Tuple[float, float]] = []
    t0 = 0.0
    while t0 < duration - 0.5:
        # primo confine di scena dentro la tolleranza; altrimenti finestra fissa
        end = next((c for c in sorted(cuts)
                    if t0 + win_min < c <= min(t0 + win_max, duration)),
                   min(t0 + target, duration))
        if duration - end < win_min:  # evita coda troppo corta
            end = duration
        wins.append((round(t0, 2), round(end, 2)))
        t0 = end
    return wins


# ---------------------------------------------------------------- prompt
def base_director_prompt() -> str:
    p = PROMPTS_DIR / "director.md"
    if p.exists():
        return p.read_text()
    return "Sei un regista esperto. Decidi le mosse di regia usando i tool forniti."


def _fmt_time(t: float) -> str:
    m, sec = divmod(int(t), 60)
    return f"{m:02d}:{sec:02d}"


def _fmt_transcript(segs: List[Dict[str, Any]]) -> str:
    if not segs:
        return "(nessun parlato in questa finestra)"
    return "\n".join(f"[{_fmt_time(s['start'])}-{_fmt_time(s['end'])}] {s['text']}" for s in segs)


def _fmt_captions(caps: List[Dict[str, Any]], n_tracks: int) -> str:
    if not caps:
        return "(nessuna descrizione disponibile)"
    lines = []
    for c in caps:
        track = c.get("track", 0)
        tag = f"[CAM {track}] " if n_tracks > 1 else ""
        spos = c.get("subject_pos", "absent")
        lines.append(f"[{_fmt_time(c['t'])}] {tag}{c['caption']} "
                     f"(inquadratura: {c.get('shot', 'medium')}; volti: {c.get('faces', 0)}; "
                     f"soggetto: {spos})")
    return "\n".join(lines)


def build_window_messages(skill: Dict[str, Any], window: Tuple[float, float],
                          transcript: List[Dict[str, Any]],
                          captions: List[Dict[str, Any]],
                          prev_summary: Optional[str],
                          video_duration: float, n_tracks: int,
                          user_note: Optional[str] = None) -> List[Dict[str, str]]:
    s = settings()
    t0, t1 = window
    allowed = skill.get("tools") or []
    limits = skill.get("limits", {})
    multi = n_tracks > 1
    registry = registry_for_prompt(allowed, multi_track=multi)

    system = f"""{base_director_prompt().strip()}

## STILE DELLA SKILL ATTIVA
Nome: {skill.get('name')} (scenario: {skill.get('scenario')})
{skill.get('description', '')}

Istruzioni di scenario (obbligatorie):
{skill.get('system_extra', '(nessuna istruzione aggiuntiva)')}

Stile: pacing {skill.get('style', {}).get('pacing')}, tono {skill.get('style', {}).get('tone')}.

## TOOL DI REGIA DISPONIBILI
{registry}

## LIMITI
- Massimo {limits.get('max_decisions', 8)} decisioni per questa finestra.
- Massimo {limits.get('max_simultaneous_overlays', 2)} sovrimpressioni visibili contemporaneamente.
- Zoom strength massimo: {limits.get('max_zoom_strength', 0.45)}.
- Ogni decisione: t (secondo assoluto d'inizio, dentro la finestra), dur (secondi), params (oggetto).
- Non ripetere lo stesso tool a raffica: alterna regia di camera ed elementi grafici.
- Se il parlato dice "guardate", "come vedete", "questo è", nomi o numeri importanti:
  supporta con zoom / overlay / polaroid. Se cita un autore o libro più di una volta:
  citation_card. Se esistono più CAM e una mostra ciò di cui si parla: camera_switch con fade.
- Gli SVG per text_overlay devono usare viewBox="0 0 800 480", testo grande e leggibile
  (font-size >= 40, colori ad alto contrasto, eventuale rettangolo di sfondo opaco).

Rispondi SOLO con il JSON structured output richiesto."""

    parts = [
        f"DURATA TOTALE DEL VIDEO: {video_duration:.1f}s",
        f"FINESTRA DA DIREGIRE: {_fmt_time(t0)} → {_fmt_time(t1)} (secondi {t0:.0f}–{t1:.0f})",
    ]
    if n_tracks > 1:
        parts.append(f"PROGETTO MULTI-CAMERA: {n_tracks} tracce. Puoi usare camera_switch.")
    if prev_summary:
        parts.append(f"RIASSUNTO DELLA FINESTRA PRECEDENTE (per continuità):\n{prev_summary}")
    parts.append(f"TRASCRIZIONE (con timecode):\n{_fmt_transcript(segments_in_window(transcript, t0, t1, s.window_overlap))}")
    parts.append(f"DESCRIZIONE FOTOGRAMMI (campionata):\n{_fmt_captions(captions_in_window(captions, t0, t1, s.window_overlap), n_tracks)}")
    if user_note:
        parts.append(f"NOTA DELL'UTENTE (segui queste indicazioni con priorità):\n{user_note}")
    parts.append("Ora decidi la regia di questa finestra: summary, mood e decisions.")

    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)}]


# ---------------------------------------------------------------- anti-overlap
def _dominant_subject_pos(captions: List[Dict[str, Any]], t0: float, t1: float) -> str:
    """Posizione dominante del soggetto nella finestra (dalle didascalie vision)."""
    from collections import Counter
    vals = [c.get("subject_pos", "absent") for c in captions
            if t0 <= c.get("t", -1) <= t1]
    vals = [v for v in vals if v in ("left", "center", "right")]
    if not vals:
        return "absent"
    return Counter(vals).most_common(1)[0][0]


def avoid_subject(decisions: List[Dict[str, Any]], captions: List[Dict[str, Any]],
                  window: Tuple[float, float]) -> None:
    """Garanzia server-side: polaroid/citazioni NON si sovrappongono al soggetto.

    Se il soggetto dominante occupa il terzo scelto per la grafica (o il centro),
    la grafica viene spostata sul terzo opposto; con soggetto al centro si usa
    sempre un lato. Modifica `decisions` in place.
    """
    t0, t1 = window
    sp = _dominant_subject_pos(captions, t0, t1)
    opposite = {"left": "right", "right": "left"}
    for d in decisions:
        if d.get("type") not in ("polaroid", "citation_card"):
            continue
        p = d.setdefault("params", {})
        pos = p.get("pos", "right")
        if sp == "center":
            if pos == "center":
                p["pos"] = "right"  # soggetto al centro → sempre un lato
        elif sp in opposite:
            if pos == sp or pos == "center":
                p["pos"] = opposite[sp]
        # sp == "absent" → lascia la scelta del regista


# ---------------------------------------------------------------- mock (demo)
_KW_POLAROID = [("auto cappottata", "auto cappottata incidente"),
                ("incidente", "incidente stradale auto"),
                ("cappotta", "auto cappottata incidente"),
                ("vista", "fatto citato dallo speaker")]
_KW_CITATION = [("dante", ("author", "Dante Alighieri ritratto")),
                ("divina commedia", ("book", "Divina Commedia copertina libro")),
                ("libro", ("book", "copertina libro classico")),
                ("autore", ("author", "scrittore ritratto storico"))]


def _mock_decisions(window: Tuple[float, float], transcript: List[Dict[str, Any]],
                    n_tracks: int, skill: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decisioni demo guidate da keyword: rende il mock utile per i test."""
    s = settings()
    t0, t1 = window
    allowed = set(skill.get("tools") or [])
    decs: List[Dict[str, Any]] = []
    text = " ".join(seg["text"].lower() for seg in segments_in_window(transcript, t0, t1, 0))
    added: List[Dict[str, Any]] = []

    def add(dtype: str, t: float, dur: float, params: Dict[str, Any], reason: str) -> None:
        if dtype not in allowed:
            return
        added.append({"type": dtype, "t": round(t, 2), "dur": round(dur, 2),
                      "params": params, "reason": reason})

    if "guardate" in text or "come vedete" in text or "attenzione" in text:
        add("zoom", t0 + 2, 3.0, {"mode": "in", "cx": 0.5, "cy": 0.42, "strength": 0.3},
            "il parlato chiede attenzione")
    if "rallenty" in text or "lento" in text or "lentissim" in text:
        add("slowmo", t0 + 13, 2.5, {"factor": 0.5}, "il parlato annuncia un rallenty")
    if "benvenuti" in text or "grazie" in text:
        add("narration", t0 + 0.5, 3.0,
            {"text": "Benvenuti su questo video raccontato da TuDirector."},
            "voce narrante di apertura")
    for kw, q in _KW_POLAROID:
        if kw in text:
            add("polaroid", t0 + 5, 5.0, {"query": q, "caption": f"«{q}»", "pos": "right"},
                f"il parlato cita: {q}")
            break
    for kw, (kind, q) in _KW_CITATION:
        if kw in text:
            add("citation_card", t0 + 10, 5.0, {"kind": kind, "query": q, "pos": "left"},
                f"cita {q}")
            break
    if n_tracks > 1 and ("attorno" in text or "dietro" in text or "spalle" in text
                         or "strada" in text):
        add("camera_switch", t0 + 15, 0.0, {"track": 1, "transition": "fade"},
            "si parla di ciò che circonda: passo alla camera posteriore")
    if "dettaglio" in text or "qui" in text:
        add("highlight_pulse", t0 + 8, 1.2, {"cx": 0.55, "cy": 0.45}, "dettaglio citato")
    if "bello" in text or "incredibile" in text or "spettacolo" in text:
        add("effects", t0 + 1, 8.0, {"saturation": 0.12, "contrast": 0.06}, "momento positivo")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 480">'
           '<rect x="120" y="360" width="560" height="90" rx="14" fill="#000000aa"/>'
           '<text x="400" y="418" text-anchor="middle" font-family="DejaVu Sans" '
           'font-size="44" font-weight="bold" fill="#ffffff">MOMENTO CHIAVE</text></svg>')
    if len(added) < 2:
        add("text_overlay", t0 + 20, 4.0, {"svg": svg, "anim": "fade"}, "sottolinea il momento")
    add("lower_third", t0 + 0.5, 4.0, {"title": "TuDirector", "subtitle": "regia AI demo"},
        "presentazione")
    if allowed & {"sticker_anim"} and ("strada" in text or "auto" in text):
        add("sticker_anim", t0 + 24, 3.0, {"kind": "car", "path": "rtl", "size": 0.2},
            "transizione giocosa")
    return added[: int(skill.get("limits", {}).get("max_decisions", 8))]


# ---------------------------------------------------------------- chiamata
def direct_window(job_dir: Any, window: Tuple[float, float],
                  transcript: List[Dict[str, Any]], captions: List[Dict[str, Any]],
                  skill: Dict[str, Any], prev_summary: Optional[str],
                  video_duration: float, n_tracks: int,
                  user_note: Optional[str] = None,
                  wid: Optional[str] = None) -> Dict[str, Any]:
    """Regia di una finestra: LLM structured output o mock. Ritorna l'oggetto window."""
    s = settings()
    t0, t1 = window
    allowed = skill.get("tools") or []
    wid = wid or f"w{int(t0 // s.window_len):03d}"

    result: Dict[str, Any] = {
        "window_id": wid, "t_start": t0, "t_end": t1,
        "summary": "", "mood": "neutro", "decisions": [], "engine": "mock",
    }

    if not llm.available():
        decisions = _mock_decisions(window, transcript, n_tracks, skill)
        decs = [d for d in (validate_decision(
            d, t0, t1, allowed, skill["limits"]["max_zoom_strength"], n_tracks > 1,
            video_duration, n_tracks) for d in decisions) if d]
        avoid_subject(decs, captions, window)
        result.update(summary="Mock: finestra demo con decisioni guidate da keyword.",
                      mood="neutro", decisions=decs)
        return result

    messages = build_window_messages(skill, window, transcript, captions, prev_summary,
                                     video_duration, n_tracks, user_note)
    schema = director_json_schema(allowed, MOODS)
    try:
        resp = llm.chat(messages, model=s.director_model, json_schema=schema,
                        temperature=0.5, max_tokens=3000)
        parsed = resp.get("parsed") or {}
        if not parsed and resp.get("content"):
            # fallback: estrai JSON dal testo
            m = resp["content"]
            b = m.find("{")
            if b >= 0:
                try:
                    parsed = __import__("json").loads(m[b:m.rfind("}") + 1])
                except Exception:
                    parsed = {}
        decisions_in = parsed.get("decisions") or []
        decs = []
        for d in decisions_in:
            v = validate_decision(d, t0, t1, allowed, skill["limits"]["max_zoom_strength"],
                                  n_tracks > 1, video_duration, n_tracks)
            if v:
                decs.append(v)
        avoid_subject(decs, captions, window)
        result.update(summary=str(parsed.get("summary", ""))[:500],
                      mood=parsed.get("mood") if parsed.get("mood") in MOODS else "neutro",
                      decisions=decs, engine="openrouter",
                      model=resp.get("model"), usage=resp.get("usage"))
    except Exception as e:
        log.error("regista LLM fallita su %s: %s — fallback mock", wid, e)
        result["error"] = str(e)[:300]
        decs = _mock_decisions(window, transcript, n_tracks, skill)
        decs = [d for d in (validate_decision(
            d, t0, t1, allowed, skill["limits"]["max_zoom_strength"], n_tracks > 1,
            video_duration, n_tracks) for d in decs) if d]
        avoid_subject(decs, captions, window)
        result["decisions"] = decs
    return result
