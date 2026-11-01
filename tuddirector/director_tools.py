"""Registro dei tool di regia disponibili al LLM-regista.

Ogni tool: id, descrizione (per il prompt), firma parametri (JSON schema-ish),
valori di default e vincoli, funzione di validazione/clamp dei parametri.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- firme
TOOLS: Dict[str, Dict[str, Any]] = {
    "zoom": {
        "label": "Zoom (in/out verso un punto)",
        "desc": "Zoom progressivo verso il punto (cx, cy) in coordinate 0..1. "
                "Usalo quando il parlato chiede attenzione o un primo piano.",
        "params": {
            "mode": {"type": "enum", "values": ["in", "out"], "default": "in"},
            "cx": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.5},
            "cy": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.45},
            "strength": {"type": "number", "min": 0.05, "max": 0.6, "default": 0.3},
            "easing": {"type": "enum", "values": ["smooth", "linear"], "default": "smooth"},
        },
        "default_dur": 3.0, "min_dur": 1.0, "max_dur": 12.0,
    },
    "pan": {
        "label": "Panoramica virtuale",
        "desc": "Deriva l'inquadratura (pan) di dx,dy in unità immagine 0..1. "
                "USALO come 'guardarsi attorno': pan ampio e lento quando il parlato "
                "parla dell'ambiente o di ciò che lo circonda, e per dare respiro "
                "tra due zoom.",
        "params": {
            "dx": {"type": "number", "min": -0.4, "max": 0.4, "default": 0.1},
            "dy": {"type": "number", "min": -0.4, "max": 0.4, "default": 0.0},
            "easing": {"type": "enum", "values": ["smooth", "linear"], "default": "smooth"},
        },
        "default_dur": 4.0, "min_dur": 1.5, "max_dur": 15.0,
    },
    "effects": {
        "label": "Effetti colore",
        "desc": "Correzioni: brightness, saturation, contrast (-1..1), vignette/grayscale/sepia on/off.",
        "params": {
            "brightness": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0},
            "saturation": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0},
            "contrast": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0},
            "vignette": {"type": "bool", "default": False},
            "grayscale": {"type": "bool", "default": False},
            "sepia": {"type": "bool", "default": False},
        },
        "default_dur": 5.0, "min_dur": 2.0, "max_dur": 30.0,
    },
    "text_overlay": {
        "label": "Testo in sovraimpressione (SVG)",
        "desc": "Sovrappone un testo evidente. Fornisci un SVG completo (viewBox 0 0 800 480) "
                "con testo e path; dichiara l'animazione: fade, slide-right, slide-up, typewriter.",
        "params": {
            "svg": {"type": "svg", "required": True},
            "anim": {"type": "enum", "values": ["fade", "slide-right", "slide-up", "typewriter"],
                     "default": "fade"},
        },
        "default_dur": 4.0, "min_dur": 1.5, "max_dur": 12.0,
    },
    "polaroid": {
        "label": "Polaroid (foto del fatto citato)",
        "desc": "Mostra come polaroid la foto di un fatto citato dallo speaker "
                "(es. 'auto cappottata' → foto di un'auto cappottata).",
        "params": {
            "query": {"type": "string", "required": True},
            "caption": {"type": "string", "default": ""},
            "pos": {"type": "enum", "values": ["left", "right", "center"], "default": "right"},
            "rotate": {"type": "number", "min": -15, "max": 15, "default": -6},
        },
        "default_dur": 5.0, "min_dur": 2.5, "max_dur": 12.0,
    },
    "citation_card": {
        "label": "Card citazione (autore/libro)",
        "desc": "Se lo speaker cita ripetutamente un autore o un libro, mostra la foto "
                "dell'autore (kind=author) o la copertina del libro (kind=book).",
        "params": {
            "kind": {"type": "enum", "values": ["author", "book"], "default": "book"},
            "query": {"type": "string", "required": True},
            "pos": {"type": "enum", "values": ["left", "right", "center"], "default": "left"},
        },
        "default_dur": 5.0, "min_dur": 2.5, "max_dur": 12.0,
    },
    "sticker_anim": {
        "label": "Animazione/sticker in movimento",
        "desc": "Un elemento grafico che attraversa lo schermo: kind=car|arrow|star, "
                "path=ltr|rtl|down-up (es. auto da destra a sinistra).",
        "params": {
            "kind": {"type": "enum", "values": ["car", "arrow", "star"], "default": "car"},
            "path": {"type": "enum", "values": ["ltr", "rtl", "down-up"], "default": "rtl"},
            "size": {"type": "number", "min": 0.1, "max": 0.5, "default": 0.22},
        },
        "default_dur": 3.0, "min_dur": 1.5, "max_dur": 8.0,
    },
    "camera_switch": {
        "label": "Cambio fotocamera (multi-traccia)",
        "desc": "Passa a un'altra traccia (track = indice partendo da 0) con cut o fade. "
                "Disponibile solo se il progetto ha più tracce.",
        "params": {
            "track": {"type": "int", "min": 0, "max": 7, "default": 1},
            "transition": {"type": "enum", "values": ["cut", "fade"], "default": "fade"},
        },
        "default_dur": 0.0, "min_dur": 0.0, "max_dur": 30.0, "instant": True,
    },
    "lower_third": {
        "label": "Banner nome/titolo (lower third)",
        "desc": "Banner elegante in basso con titolo e sottotitolo.",
        "params": {
            "title": {"type": "string", "required": True},
            "subtitle": {"type": "string", "default": ""},
        },
        "default_dur": 4.0, "min_dur": 2.0, "max_dur": 10.0,
    },
    "highlight_pulse": {
        "label": "Pulse di attenzione",
        "desc": "Breve zoom-pulse verso (cx, cy) per evidenziare un dettaglio.",
        "params": {
            "cx": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.5},
            "cy": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.5},
            "strength": {"type": "number", "min": 0.05, "max": 0.35, "default": 0.15},
        },
        "default_dur": 1.2, "min_dur": 0.6, "max_dur": 3.0,
    },
    "slowmo": {
        "label": "Rallenty",
        "desc": "Rallenta il passaggio (factor 0.5 = metà velocità). Il contenuto del "
                "rallenty viene rallentato davvero (video+audio sincronizzati) accorciando "
                "leggermente la durata totale. Da usare con parsimonia.",
        "params": {
            "factor": {"type": "number", "min": 0.5, "max": 0.9, "default": 0.5},
        },
        "default_dur": 2.0, "min_dur": 1.0, "max_dur": 6.0,
    },
    "narration": {
        "label": "Voce narrante (TTS)",
        "desc": "Una voce fuori campo legge il testo indicato (max ~20 parole): utile per "
                "introduzioni, chiusure o richiami. Il testo viene anche mostrato? No: solo audio.",
        "params": {
            "text": {"type": "string", "required": True},
            "speed": {"type": "number", "min": 0.5, "max": 1.5, "default": 1.0},
        },
        "default_dur": 3.0, "min_dur": 1.0, "max_dur": 12.0,
    },
}

TOOL_IDS = set(TOOLS.keys())


def registry_for_prompt(allowed: List[str], multi_track: bool = False) -> str:
    """Descrizione testuale dei tool per il prompt del regista."""
    lines = []
    for tid in allowed:
        t = TOOLS.get(tid)
        if not t:
            continue
        params = ", ".join(
            f"{k}{'*' if v.get('required') else ''}:{v['type']}"
            for k, v in t["params"].items())
        note = " (DISPONIBILE: progetto multi-traccia)" if tid == "camera_switch" and multi_track \
            else (" (NON disponibile: una sola traccia)" if tid == "camera_switch" and not multi_track
                  else "")
        lines.append(f"- {tid}: {t['desc']} Parametri: {params} Durata consigliata: "
                     f"{t['default_dur']}s{note}")
    return "\n".join(lines)


def params_schema(tid: str) -> Dict[str, Any]:
    t = TOOLS[tid]
    props, required = {}, []
    for k, v in t["params"].items():
        if v["type"] == "enum":
            props[k] = {"type": "string", "enum": v["values"]}
        elif v["type"] == "number":
            props[k] = {"type": "number"}
        elif v["type"] == "int":
            props[k] = {"type": "integer"}
        elif v["type"] == "bool":
            props[k] = {"type": "boolean"}
        else:
            props[k] = {"type": "string"}
        if v.get("required"):
            required.append(k)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": True}


def _svg_has_visible_text(svg: str) -> bool:
    """True se l'SVG contiene testo visibile (contenuto tra tag)."""
    if not svg or not svg.strip():
        return False
    texts = re.findall(r">([^<>]+)<", svg)
    return any(t.strip() for t in texts)


def director_json_schema(allowed: List[str], moods: List[str]) -> Dict[str, Any]:
    """Schema structured output per una finestra di regia."""
    return {
        "name": "director_window",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "cosa accade nella finestra"},
                "mood": {"type": "string", "enum": moods},
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": sorted(allowed)},
                            "t": {"type": "number"},
                            "dur": {"type": "number"},
                            "params": params_schema(sorted(allowed)[0]) if len(allowed) == 1
                                      else {"type": "object", "additionalProperties": True},
                            "reason": {"type": "string"},
                        },
                        "required": ["type", "t", "dur", "params"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["summary", "mood", "decisions"],
            "additionalProperties": False,
        },
    }


# ---------------------------------------------------------------- validazione
_SVG_OK = re.compile(r"<\s*(script|foreignObject|iframe|object|embed|use)", re.I)
_SVG_OK_TAGS = re.compile(r"<\s*/?\s*(svg|g|defs|title|text|tspan|path|rect|circle|ellipse|"
                          r"line|polyline|polygon|style|linearGradient|radialGradient)\b", re.I)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def validate_decision(dec: Dict[str, Any], window_t0: float, window_t1: float,
                      allowed: List[str], max_zoom: float, multi_track: bool,
                      video_duration: float, n_tracks: int,
                      reject: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Valida una decisione del LLM; ritorna la decisione clampata o None se scartata.

    Se `reject` (dict) è passato, viene popolato con {type, reason} quando scarta.
    """
    def _rej(reason: str) -> None:
        if reject is not None:
            reject["reason"] = reason
            reject["type"] = str(dec.get("type", "?")) if isinstance(dec, dict) else "?"

    if not isinstance(dec, dict):
        _rej("decisione non è un oggetto")
        return None
    dtype = str(dec.get("type", "")).strip()
    if dtype not in allowed:
        _rej(f"tipo '{dtype}' non consentito dalla skill")
        return None
    tool = TOOLS[dtype]
    if dtype == "camera_switch" and not multi_track:
        _rej("camera_switch ma il progetto ha una sola traccia")
        return None

    try:
        t = float(dec.get("t", window_t0))
    except (TypeError, ValueError):
        _rej("tempo 't' non numerico")
        return None
    dur = float(dec.get("dur", tool.get("default_dur", 3.0)) or 0.0)
    if not tool.get("instant"):
        dur = _clamp(dur, tool.get("min_dur", 0.5), tool.get("max_dur", 30.0))
    else:
        dur = 0.0
    # tolleranza overlap: consentito uscire leggermente dalla finestra
    if t < window_t0 - 5.5 or t > window_t1:
        _rej(f"t={t:.1f}s fuori dalla finestra [{window_t0:.1f}–{window_t1:.1f}]")
        return None
    t = _clamp(t, 0.0, max(0.0, video_duration - max(0.5, dur)))
    if not tool.get("instant"):
        end = t + dur
        if end > video_duration:
            dur = max(0.5, video_duration - t)

    params_in = dec.get("params") or {}
    if not isinstance(params_in, dict):
        params_in = {}
    params: Dict[str, Any] = {}
    for k, spec in tool["params"].items():
        raw = params_in.get(k, spec.get("default"))
        if raw is None:
            raw = spec.get("default")
        if spec["type"] == "number":
            try:
                params[k] = _clamp(float(raw), spec.get("min", -1e9), spec.get("max", 1e9))
            except (TypeError, ValueError):
                params[k] = spec.get("default")
        elif spec["type"] == "int":
            try:
                params[k] = int(_clamp(float(raw), spec.get("min", 0), spec.get("max", 1e9)))
            except (TypeError, ValueError):
                params[k] = spec.get("default")
        elif spec["type"] == "bool":
            params[k] = bool(raw) if not isinstance(raw, str) else raw.lower() in ("1", "true", "yes", "on")
        elif spec["type"] == "enum":
            params[k] = raw if raw in spec["values"] else spec.get("default")
        elif spec["type"] == "svg":
            params[k] = str(raw or "")
        else:
            params[k] = str(raw or "")

    # ── parametri obbligatori NON vuoti: mai overlay vuoti a schermo ─────────
    for k, spec in tool["params"].items():
        if spec.get("required") and not str(params.get(k, "")).strip():
            _rej(f"parametro obbligatorio '{k}' vuoto (mai overlay vuoti)")
            return None

    if dtype == "zoom":
        params["strength"] = _clamp(params.get("strength", 0.3), 0.05, max_zoom)
    if dtype == "camera_switch":
        params["track"] = _clamp(int(params.get("track", 1)), 0, max(0, n_tracks - 1))
        if params["track"] == 0:
            _rej("camera_switch verso la traccia già attiva")
            return None
    if dtype == "text_overlay":
        svg = params.get("svg", "")
        if _SVG_OK.search(svg) or not _SVG_OK_TAGS.search(svg):
            _rej("SVG contiene elementi non consentiti o malformato")
            return None
        if not _svg_has_visible_text(svg):
            _rej("SVG senza testo visibile (niente banner vuoti)")
            return None
        if len(svg) > 20000:
            svg = svg[:20000]
            params["svg"] = svg

    out = {"type": dtype, "t": round(t, 2), "dur": round(dur, 2), "params": params}
    reason = str(dec.get("reason") or "")[:300]
    if reason:
        out["reason"] = reason
    return out
