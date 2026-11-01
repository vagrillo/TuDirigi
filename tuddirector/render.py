"""Renderer: traduce le decisioni di regia in un filtergraph ffmpeg.

Architettura:
- Traccia singola (caso comune): UNA passata.
  * Con `slowmo`: timeline segmentata — trim/setpts/concat del video e
    atrim/atempo/concat dell'audio (rallenty reale, A/V sincronizzati, durata
    totale accorciata); le decisioni vengono rimappate sul tempo di uscita.
  * Senza slowmo: catena lineare come prima.
- Le narrazioni (TTS) vengono mixate nell'audio con adelay+amix.
- Multi-camera: per ogni traccia una passata video-only (zoom+effetti; slowmo
  approssimato a freeze per semplicità), poi assemblaggio (xfade/concat per
  camera_switch) + overlays + mix narrazioni + audio della traccia 0.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import settings
from .overlay import prepare_overlays

log = logging.getLogger("tuddirector.render")


class RenderError(Exception):
    pass


# ---------------------------------------------------------------- helpers espressioni
def _ease(u: str) -> str:
    """smootherstep C2: pow(u,3)*(u*(6u-15)+10)"""
    return f"(pow({u},3)*({u}*(6*{u}-15)+10))"


def _clip01(u: str) -> str:
    return f"clip({u},0,1)"


class _ZoomExpr:
    """Costruisce le espressioni z/x/y per zoompan.

    T (tempo in secondi) è calcolato come (on/fps): zoompan valuta per frame di uscita.
    Formula centro: x = cx*iw - iw/(2*zoom) (clamped), con offset pan additivo.
    """

    def __init__(self, fps: int) -> None:
        self.fps = fps
        self.z_terms: List[str] = []
        self.pan_x_terms: List[str] = []
        self.pan_y_terms: List[str] = []
        self.windows: List[Tuple[float, float, float, float]] = []  # t_on, t_off, cx, cy

    def _T(self, t: float) -> str:
        return f"((on/{self.fps})-({t:.3f}))"

    def add_zoom_in(self, t0: float, d: float, strength: float, cx: float, cy: float) -> None:
        u1 = _clip01(f"{self._T(t0)}/{d:.3f}")
        self.z_terms.append(f"{strength:.3f}*{_ease(u1)}")
        u2 = _clip01(f"{self._T(t0 + d)}/{d / 2:.3f}")
        self.z_terms.append(f"-{strength:.3f}*{_ease(u2)}")
        self.windows.append((t0, t0 + 1.5 * d, cx, cy))

    def add_zoom_out(self, t0: float, d: float, strength: float, cx: float, cy: float) -> None:
        # ramp-up invisibile prima della finestra, discesa visibile durante
        u0 = _clip01(f"{self._T(t0 - d)}/{d:.3f}")
        self.z_terms.append(f"{strength:.3f}*{_ease(u0)}")
        u1 = _clip01(f"{self._T(t0)}/{d:.3f}")
        self.z_terms.append(f"-{strength:.3f}*{_ease(u1)}")
        self.windows.append((t0 - d, t0 + d, cx, cy))

    def add_pan(self, t0: float, d: float, dx: float, dy: float) -> None:
        implicit = 0.18  # zoom implicito per avere margine di pan
        u1 = _clip01(f"{self._T(t0)}/{d:.3f}")
        self.z_terms.append(f"{implicit}*{_ease(u1)}")
        u2 = _clip01(f"{self._T(t0 + d)}/{d / 2:.3f}")
        self.z_terms.append(f"-{implicit}*{_ease(u2)}")
        e = _ease(u1)
        self.pan_x_terms.append(f"gte(on/{self.fps},{t0:.3f})*({dx:.4f}*{e})*iw")
        self.pan_y_terms.append(f"gte(on/{self.fps},{t0:.3f})*({dy:.4f}*{e})*ih")

    def build(self) -> Optional[Tuple[str, str, str]]:
        if not self.z_terms:
            return None
        z = "1+" + "+".join(self.z_terms)
        # centro della zoom attiva: ultima finestra attiva, default 0.5/0.45
        cx_expr, cy_expr = "0.5", "0.45"
        for t_on, t_off, cx, cy in sorted(self.windows, key=lambda w: w[0]):
            cond = f"gte(on/{self.fps},{t_on:.3f})*lt(on/{self.fps},{t_off:.3f})"
            cx_expr = f"if({cond},{cx:.4f},{cx_expr})"
            cy_expr = f"if({cond},{cy:.4f},{cy_expr})"
        pan_x = "+".join(self.pan_x_terms) if self.pan_x_terms else "0"
        pan_y = "+".join(self.pan_y_terms) if self.pan_y_terms else "0"
        x = (f"clip({cx_expr}*iw-iw/(2*zoom)+{pan_x},0,iw-iw/zoom)")
        y = (f"clip({cy_expr}*ih-ih/(2*zoom)+{pan_y},0,ih-ih/zoom)")
        return z, x, y


def _eq_expr(p: Dict[str, Any]) -> Optional[str]:
    parts = []
    if "brightness" in p and p["brightness"]:
        parts.append(f"brightness={p['brightness']:.3f}")
    if "saturation" in p and p["saturation"]:
        parts.append(f"saturation={max(0.0, 1.0 + p['saturation']):.3f}")
    if "contrast" in p and p["contrast"]:
        parts.append(f"contrast={max(0.0, 1.0 + p['contrast']):.3f}")
    return "eq=" + ":".join(parts) if parts else None


def _overlay_pos(pos: str) -> str:
    """Posizione X degli overlay: sempre con margine, mai centrati sul soggetto."""
    return {"left": "40", "right": "W-w-40", "center": "(W-w)/2"}.get(pos, "(W-w)/2")


def _overlay_y_bottom() -> str:
    """Posizione Y degli overlay: ancorata IN BASSO (non copre il soggetto)."""
    return "H-h-40"


def _sticker_xy(path_expr_kind: str, t0: float, dur: float) -> Tuple[str, str]:
    # l'attraversata avviene nella fascia bassa dello schermo
    y_bottom = "'H-h-40'"
    if path_expr_kind == "ltr":
        return (f"'-w+(W+w)*(t-{t0:.3f})/{max(dur, 0.1):.3f}'", y_bottom)
    if path_expr_kind == "down-up":
        return (f"'(W-w)/2'", f"'H-(H+h)*(t-{t0:.3f})/{max(dur, 0.1):.3f}'")
    return (f"'W-(W+w)*(t-{t0:.3f})/{max(dur, 0.1):.3f}'", y_bottom)


def _audio_offset_chain() -> List[str]:
    """Filtri per compensare il desync della sorgente (settings.audio_offset_ms).

    >0 ritarda l'audio (voce più tardi), <0 lo anticipa (taglia l'inizio).
    """
    s = settings()
    ms = int(getattr(s, "audio_offset_ms", 0) or 0)
    if ms > 0:
        return [f"adelay={ms}|{ms}"]
    if ms < 0:
        return [f"atrim=start={-ms / 1000:.3f}", "asetpts=PTS-STARTPTS"]
    return []


# ---------------------------------------------------------------- timeline slowmo
def build_timeline(duration: float, slowmos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Costruisce la timeline segmentata per i rallenty reali.

    Ritorna {"segments": [{src:(a,b), f, out_start, out_len}], "out_duration", "slowmo"}.
    Ogni slowmo consuma dur*factor secondi di sorgente e produce `dur` secondi di uscita.
    """
    segments: List[Dict[str, Any]] = []
    t = 0.0
    for sm in sorted(slowmos, key=lambda d: d["t"]):
        t0 = float(sm["t"])
        dur = max(1.0, float(sm["dur"]))
        f = max(0.5, min(0.9, float(sm.get("params", {}).get("factor", 0.5))))
        if t0 <= t + 0.3 or t0 + dur * f > duration - 0.2:
            continue  # sovrapposto o troppo vicino alla fine: scartato
        if t0 > t:
            segments.append({"src": (t, t0), "f": 1.0})
        segments.append({"src": (t0, t0 + dur * f), "f": f})
        t = t0 + dur * f
    if t < duration:
        segments.append({"src": (t, duration), "f": 1.0})
    o = 0.0
    for seg in segments:
        seg["out_start"] = round(o, 3)
        seg["out_len"] = round((seg["src"][1] - seg["src"][0]) / seg["f"], 3)
        o += seg["out_len"]

    def remap(t_src: float) -> float:
        for seg in segments:
            a, b = seg["src"]
            if t_src < b or seg is segments[-1]:
                return round(seg["out_start"] + (max(t_src, a) - a) / seg["f"], 3)
        return round(o, 3)

    return {"segments": segments, "out_duration": round(o, 3), "slowmo": bool(slowmos),
            "remap": remap}


def _segment_video_graph(segments: List[Dict[str, Any]], fps: int) -> str:
    """[pf] → split → trim/setpts → concat = [vcat] (tempo di uscita)."""
    n = len(segments)
    if n == 1:
        return ""
    labels = [f"sv{i}" for i in range(n)]
    parts = [f"[pf]split={n}[" + "][".join(labels) + "]"]
    vlabels = []
    for i, seg in enumerate(segments):
        a, b = seg["src"]
        f = seg["f"]
        vl = f"vg{i}"
        parts.append(f"[{labels[i]}]trim=start={a:.3f}:end={b:.3f},"
                     f"setpts=(PTS-STARTPTS)/{f:.3f}[{vl}]")
        vlabels.append(f"[{vl}]")
    parts.append("".join(vlabels) + f"concat=n={n}:v=1:a=0[vcat]")
    return ";".join(parts)


def _segment_audio_graph(segments: List[Dict[str, Any]]) -> Optional[str]:
    """[ar] → split → atrim/asetpts/atempo → concat = [aout] (A/V sincronizzati)."""
    n = len(segments)
    if n == 1:
        return None
    labels = [f"sa{i}" for i in range(n)]
    parts = [f"[ar]asplit={n}[" + "][".join(labels) + "]"]
    alabels = []
    for i, seg in enumerate(segments):
        a, b = seg["src"]
        f = seg["f"]
        al = f"ag{i}"
        tempo = "" if abs(f - 1.0) < 1e-3 else f",atempo={f:.3f}"
        parts.append(f"[{labels[i]}]atrim=start={a:.3f}:end={b:.3f},"
                     f"asetpts=PTS-STARTPTS{tempo}[{al}]")
        alabels.append(f"[{al}]")
    parts.append("".join(alabels) + f"concat=n={n}:v=0:a=1[aout]")
    return ";".join(parts)


def _narration_mix(narrations: List[Dict[str, Any]], audio_idx_start: int
                   ) -> Tuple[List[str], List[str], str]:
    """Catena adelay+amix per i clip TTS. Ritorna (fc_parts, input_args_flat, label)."""
    parts: List[str] = []
    inputs: List[str] = []
    base_idx = audio_idx_start
    if not narrations:
        return parts, inputs, ""
    mix_labels = []
    for k, d in enumerate(narrations):
        t_out = d.get("_out_t", d["t"])
        ms = max(0, int(t_out * 1000))
        parts.append(f"[{base_idx + k}:a]aresample=44100,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"adelay={ms}|{ms}[na{k}]")
        mix_labels.append(f"[na{k}]")
        inputs += ["-i", d["params"]["_tts_path"]]
    return parts, inputs, "narbase"


# ---------------------------------------------------------------- analisi decisioni
def _group_by_track(decisions: List[Dict[str, Any]], switch_plan: List[float]) -> Dict[int, List]:
    """Assegna le decisioni di camera alla traccia attiva in quel momento."""
    bounds = sorted(set([0.0] + switch_plan))

    def active_track(t: float) -> int:
        k = 0
        for i, b in enumerate(bounds):
            if t >= b:
                k = i
        return k

    groups: Dict[int, List] = {}
    for d in decisions:
        if d["type"] == "camera_switch":
            continue
        k = active_track(d["t"])
        groups.setdefault(k, []).append(d)
    return groups


def _build_track_graph(d_list: List[Dict[str, Any]], duration: float, fps: int,
                       with_fps: bool = True) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Filtergraph di camera per una traccia: [prescale → zoompan → effetti].

    with_fps=False omette il filtro fps (già applicato a monte, es. dopo concat).
    Nella modalità multi-camera `slowmo` è approssimato a freeze-frame.
    Ritorna (chain, freeze_decisions).
    """
    s = settings()
    chain: List[str] = ([f"fps={fps}"] if with_fps else [])
    chain += [f"scale={s.out_w * 4}:-2:flags=bicubic"]
    zooms = _ZoomExpr(fps)
    freeze: List[Dict[str, Any]] = []
    for d in d_list:
        p = d["params"]
        if d["type"] == "zoom":
            (zooms.add_zoom_in if p.get("mode", "in") == "in"
             else zooms.add_zoom_out)(d["t"], d["dur"], float(p.get("strength", 0.3)),
                                      float(p.get("cx", 0.5)), float(p.get("cy", 0.45)))
        elif d["type"] == "pan":
            zooms.add_pan(d["t"], d["dur"], float(p.get("dx", 0.1)), float(p.get("dy", 0.0)))
        elif d["type"] == "highlight_pulse":
            zooms.add_zoom_in(d["t"], d["dur"], float(p.get("strength", 0.15)),
                              float(p.get("cx", 0.5)), float(p.get("cy", 0.5)))
        elif d["type"] == "slowmo":
            freeze.append(d)
    zexpr = zooms.build()
    if zexpr:
        z, x, y = zexpr
        chain.append(
            f"zoompan=z='{z}':x='clip({x},0,iw-iw/zoom)':y='clip({y},0,ih-ih/zoom)'"
            f":d=1:s={s.out_w}x{s.out_h}:fps={fps}"
        )
    else:
        chain.append(f"scale={s.out_w}:{s.out_h}:force_original_aspect_ratio=increase,"
                     f"crop={s.out_w}:{s.out_h}")
    for d in d_list:
        if d["type"] == "effects":
            e = _eq_expr(d["params"])
            if e:
                chain.append(f"{e}:enable='between(t,{d['t']:.3f},{d['t'] + d['dur']:.3f})'")
            if d["params"].get("vignette"):
                chain.append(f"vignette=enable='between(t,{d['t']:.3f},{d['t'] + d['dur']:.3f})'")
            if d["params"].get("grayscale"):
                chain.append(f"hue=s=0:enable='between(t,{d['t']:.3f},{d['t'] + d['dur']:.3f})'")
            if d["params"].get("sepia"):
                chain.append(f"colorchannelmixer=rr=0.9:rg=0.05:rb=-0.2:gg=0.1:gb=-0.1:"
                             f"enable='between(t,{d['t']:.3f},{d['t'] + d['dur']:.3f})'")
    return chain, freeze


# ---------------------------------------------------------------- ffmpeg runner
def _run_ffmpeg(cmd: List[str], progress_cb: Optional[Callable] = None,
                total: float = 1.0) -> None:
    cmd = cmd + ["-nostats", "-progress", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if progress_cb and line.startswith("out_time_ms="):
            try:
                us = float(line.split("=", 1)[1].strip())
                progress_cb(min(0.99, (us / 1e6) / max(total, 0.1)))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read()[-800:] if proc.stderr else ""
        raise RenderError(f"ffmpeg exit {proc.returncode}: {err}")


def _encode_args(crf: int = 20) -> List[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def render_track(src: Path, out: Path, d_list: List[Dict[str, Any]], duration: float,
                 progress_cb=None, fps: int = 25) -> Path:
    """Passata video-only per una traccia (zoom + effetti; slowmo→freeze)."""
    s = settings()
    graph, freeze = _build_track_graph(d_list, duration, fps)
    inputs: List[str] = ["-i", str(src)]
    extra: List[Tuple[Dict[str, Any], int]] = []
    fi = 1
    tmp = out.parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for d in freeze:
        png = tmp / f"freeze-{fi:02d}.png"
        subprocess.run(["ffmpeg", "-y", "-ss", str(d["t"]), "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={s.out_w}:{s.out_h}",
                        str(png)], capture_output=True, timeout=120)
        if png.exists():
            inputs += ["-loop", "1", "-t", f"{d['dur']:.3f}", "-i", str(png)]
            extra.append((d, fi))
            fi += 1
    fc = f"[0:v]{','.join(graph)}[base]"
    last = "base"
    for k, (d, idx) in enumerate(extra):
        nxt = f"fz{k}"
        fc += (f";[{idx}:v]format=rgba,setpts=PTS-STARTPTS+{d['t']:.3f}/TB[fo{k}];"
               f"[{last}][fo{k}]overlay=0:0:enable='between(t,{d['t']:.3f},"
               f"{d['t'] + d['dur']:.3f})'[{nxt}]")
        last = nxt
    fc += f";[{last}]format=yuv420p[vout]"
    cmd = (["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[vout]",
            "-t", f"{duration:.3f}", "-r", str(fps)] + _encode_args() + [str(out)])
    _run_ffmpeg(cmd, progress_cb, duration)
    return out


def _assembly_graph(spans: List[Tuple[float, float, int]], inter: Dict[int, Path],
                    transitions: List[Tuple[str, float]], fade: float = 0.6
                    ) -> Tuple[str, str]:
    """Catena xfade/concat sui render intermedi. Ritorna (filtergraph, last_label)."""
    fc_parts: List[str] = []
    segs = []
    for i, (a, b, tr) in enumerate(spans):
        fc_parts.append(f"[{tr}:v]trim=start={a:.3f}:end={b:.3f},"
                        f"setpts=PTS-STARTPTS[seg{i}]")
        segs.append(f"[seg{i}]")
    dur = lambda ab: ab[1] - ab[0]
    if len(segs) == 1:
        fc_parts.append(f"{segs[0]}null[asm]")
        return ";".join(fc_parts), "asm"
    acc = dur(spans[0])
    fc_parts.append(f"{segs[0]}null[c0]")
    cur = "c0"
    for i in range(1, len(spans)):
        nxt = f"c{i}" if i < len(spans) - 1 else "asm"
        trans, tdur = (transitions[i - 1] if i - 1 < len(transitions)
                       else ("fade", fade))
        if trans == "fade" and acc > tdur and dur(spans[i]) > tdur:
            fc_parts.append(f"[{cur}]{segs[i]}xfade=transition=fade:duration={tdur:.2f}:"
                            f"offset={acc - tdur:.3f}[{nxt}]")
            acc = acc + dur(spans[i]) - tdur
        else:
            fc_parts.append(f"[{cur}]{segs[i]}concat=n=2:v=1:a=0[{nxt}]")
            acc = acc + dur(spans[i])
        cur = nxt
    return ";".join(fc_parts), "asm"


# ---------------------------------------------------------------- render job
def render_job(job: Dict[str, Any], job_dir: Path, progress_cb=None) -> Path:
    """Render completo del job. Ritorna il path del file finale."""
    s = settings()
    out_dir = job_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "final.mp4"
    # fps di uscita: per-job (auto = fedele alla sorgente) o da settings
    fps = int(job.get("out_fps") or 0)
    if not fps:
        fps = int(s.out_fps or 0)
    if not fps:
        # auto: fps del primo stream video (anche per job vecchi senza out_fps)
        fps = int(round(min(60, max(24, (job.get("tracks") or [{}])[0].get("fps") or 25))))

    tracks = job.get("tracks", [])
    if not tracks:
        raise RenderError("nessuna traccia nel job")
    duration = min(t["duration"] for t in tracks if t.get("duration", 0) > 0) or 0
    if duration <= 0:
        raise RenderError("durata non valida")

    decisions: List[Dict[str, Any]] = []
    for w in job.get("windows", []):
        decisions.extend(w.get("decisions", []))
    decisions.sort(key=lambda d: d["t"])
    skill = job.get("_skill_preset") or {}
    if skill:
        base = dict(skill)
        base.setdefault("saturation", 0)
        base.setdefault("contrast", 0)
        base.setdefault("brightness", 0)
        if any(base.values()):
            decisions = [{"type": "effects", "t": 0.0, "dur": duration, "params": base}] + decisions

    prepare_overlays(decisions, job_dir)

    switches = [d for d in decisions if d["type"] == "camera_switch"]
    switch_times = sorted(d["t"] for d in switches if 0.5 < d["t"] < duration - 0.5)
    src0 = job_dir / tracks[0]["path"]

    if not switch_times:
        return _render_single(job_dir, src0, decisions, duration, tracks, final,
                              progress_cb, fps)

    # ------- multi-camera: per-traccia intermedi + assemblaggio + overlays
    return _render_multicam(job_dir, src0, decisions, duration, tracks, switches,
                            switch_times, final, progress_cb, fps)


# ---------------------------------------------------------------- single track
def _render_single(job_dir: Path, src0: Path, decisions: List[Dict[str, Any]],
                   duration: float, tracks: List[Dict[str, Any]], final: Path,
                   progress_cb=None, fps: int = 25) -> Path:
    """Una passata: timeline (slowmo reale) → camera → overlays → audio + narrazioni."""
    s = settings()
    slowmos = [d for d in decisions if d["type"] == "slowmo"]
    timeline = build_timeline(duration, slowmos)
    out_dur = timeline["out_duration"]
    remap = timeline["remap"]

    # decisioni camera (senza slowmo) rimappate sul tempo di uscita
    cam_decisions = []
    for d in decisions:
        if d["type"] == "slowmo":
            continue
        d2 = dict(d)
        d2["t"] = remap(d["t"])
        cam_decisions.append(d2)
    cam_decisions.sort(key=lambda d: d["t"])
    graph, freeze_unused = _build_track_graph(cam_decisions, out_dur, fps,
                                              with_fps=False)

    inputs: List[str] = ["-i", str(src0)]
    fc_parts: List[str] = []

    # ---- video: timeline segmentata + catena camera
    segs = timeline["segments"]
    if timeline["slowmo"]:
        # Rallenty reale: la catena camera è applicata PER SEGMENTO in tempo
        # locale-sorgente, poi il segmento viene stirato (setpts/f) e concatenato.
        # zoompan ri-temporizza a fps uniformi, quindi lo stretch DEVE avvenire
        # dopo zoompan (prima del concat), altrimenti viene annullato.
        s_out = settings()
        fc_parts.append(f"[0:v]fps={fps}[pf]")
        n = len(segs)
        split_labels = [f"sp{k}" for k in range(n)]
        fc_parts.append(";" + f"[pf]split={n}[" + "][".join(split_labels) + "]")
        seg_labels = []
        for k, seg in enumerate(segs):
            a, b = seg["src"]
            f = seg["f"]
            out_a, out_len = seg["out_start"], seg["out_len"]
            # decisioni camera che intersecano il segmento, in tempo locale-sorgente
            seg_ds: List[Dict[str, Any]] = []
            for d in cam_decisions:
                if d["type"] not in ("zoom", "pan", "highlight_pulse", "effects"):
                    continue
                t0s = max(d["t"], out_a)
                t1s = min(d["t"] + d["dur"], out_a + out_len)
                if t1s - t0s <= 0.05:
                    continue
                d2 = dict(d)
                d2["t"] = round((t0s - out_a) * f, 3)
                d2["dur"] = round((t1s - t0s) * f, 3)
                seg_ds.append(d2)
            graph_k, _ = _build_track_graph(seg_ds, b - a, fps, with_fps=False)
            sk = f"sg{k}"
            fc_parts.append(";" + f"[{split_labels[k]}]trim=start={a:.3f}:end={b:.3f},"
                                  f"setpts=PTS-STARTPTS{',' if graph_k else ''}"
                                  f"{','.join(graph_k)}"
                                  f",setpts=(PTS-STARTPTS)/{f:.3f}[{sk}]")
            seg_labels.append(f"[{sk}]")
        fc_parts.append(";" + "".join(seg_labels) + f"concat=n={n}:v=1:a=0[vcat]")
        video_src = "vcat"
        graph = ["null"]  # camera già applicata per segmento: solo pass-through
    else:
        video_src = "0:v"
    fc_parts.append((";" if fc_parts else "") + f"[{video_src}]{','.join(graph)}[base]")

    # ---- overlays
    ov_list = [d for d in cam_decisions if d["type"] in
               ("text_overlay", "lower_third", "polaroid", "citation_card", "sticker_anim")
               and d.get("params", {}).get("_png")]
    fi = 1
    last = "base"
    for d in ov_list:
        inputs += ["-loop", "1", "-t", f"{max(d['dur'], 0.4):.3f}", "-i", d["params"]["_png"]]
        nxt = f"ov{fi}"
        dur = max(d["dur"], 0.4)
        fadeout = f"fade=t=out:st={dur - 0.35:.3f}:d=0.35:alpha=1," if dur > 0.8 else ""
        fc_parts.append(
            f";[{fi}:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1,{fadeout}"
            f"setpts=PTS-STARTPTS+{d['t']:.3f}/TB[oi{fi}]")
        p = d["params"]
        if d["type"] == "sticker_anim":
            x, y = _sticker_xy(p.get("path", "rtl"), d["t"], d["dur"])
        elif d["type"] in ("polaroid", "citation_card"):
            x, y = f"'{_overlay_pos(p.get('pos', 'right'))}'", f"'{_overlay_y_bottom()}'"
        else:
            x, y = "0", "0"
        fc_parts.append(f";[{last}][oi{fi}]overlay=x={x}:y={y}:"
                        f"enable='between(t,{d['t']:.3f},{d['t'] + d['dur']:.3f})'[{nxt}]")
        last = nxt
        fi += 1
    fc_parts.append(f";[{last}]format=yuv420p[vout]")

    # ---- audio: segmenti (slowmo) + narrazioni
    narrations = [d for d in decisions if d["type"] == "narration"
                  and d.get("tts_clip")]
    for d in narrations:
        d["params"]["_tts_path"] = str(job_dir / d["tts_clip"])
        d["_out_t"] = remap(d["t"])

    has_audio = bool(tracks[0].get("has_audio"))
    audio_label = None
    off_chain = _audio_offset_chain()
    off_str = ("," + ",".join(off_chain)) if off_chain else ""
    if has_audio:
        fc_parts.append(f";[0:a]aresample=44100,"
                        f"aformat=sample_fmts=fltp:channel_layouts=stereo{off_str}[ar]")
        seg_audio = _segment_audio_graph(segs)
        audio_label = "aout" if seg_audio else "ar"
        if seg_audio:
            fc_parts.append(";" + seg_audio)
    elif narrations:
        # base silenziosa della durata di uscita
        inputs += ["-f", "lavfi", "-t", f"{out_dur:.3f}",
                   "-i", "anullsrc=r=44100:cl=stereo"]
        audio_label = f"an{fi}"
        fc_parts.append(f";[{fi}:a]aformat=sample_fmts=fltp:channel_layouts=stereo"
                        f"[{audio_label}]")
        fi += 1

    mix_parts, tts_inputs, _ = _narration_mix(narrations, fi)
    if mix_parts:
        inputs += tts_inputs
        fc_parts.append(";" + ";".join(mix_parts))
        mix_in = [audio_label] + [f"na{k}" for k in range(len(narrations))]
        audio_label = "afinal"
        fc_parts.append(";" + "".join(f"[{l}]" for l in mix_in)
                        + f"amix=inputs={len(mix_in)}:normalize=0:duration=longest"
                          f"[{audio_label}]")

    if not audio_label:
        # nessuna sorgente audio: silenzio (input in fase input, mai dopo le opzioni output)
        fc_parts.append(f";[{fi}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[asilent]")
        inputs += ["-f", "lavfi", "-t", f"{out_dur:.3f}",
                   "-i", "anullsrc=r=44100:cl=stereo"]
        audio_label = "asilent"

    fc = "".join(fc_parts)

    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[vout]",
            "-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-t", f"{out_dur:.3f}", "-r", str(fps)] + _encode_args() + [str(final)]
    _run_ffmpeg(cmd, progress_cb, out_dur)
    return final


# ---------------------------------------------------------------- multicam
def _render_multicam(job_dir: Path, src0: Path, decisions: List[Dict[str, Any]],
                     duration: float, tracks: List[Dict[str, Any]],
                     switches: List[Dict[str, Any]], switch_times: List[float],
                     final: Path, progress_cb=None, fps: int = 25) -> Path:
    s = settings()
    switches_sorted = sorted([d for d in switches if 0.5 < d["t"] < duration - 0.5],
                             key=lambda d: d["t"])
    bounds = [0.0] + [d["t"] for d in switches_sorted]
    spans = []
    for i, b in enumerate(bounds):
        e = bounds[i + 1] if i + 1 < len(bounds) else duration
        if e - b > 0.2:
            tr = 0 if i == 0 else int(switches_sorted[i - 1]["params"].get("track", 1))
            tr = min(tr, len(tracks) - 1)
            spans.append((b, e, tr))
    spans_meta = []
    for i in range(1, len(spans)):
        sd = switches_sorted[i - 1]
        spans_meta.append((sd["params"].get("transition", "fade"),
                           min(0.6, (spans[i][1] - spans[i][0]) / 2,
                               (spans[i - 1][1] - spans[i - 1][0]) / 2)))

    tracks_used = sorted(set(sp[2] for sp in spans))
    inter: Dict[int, Path] = {}
    groups = _group_by_track(decisions, switch_times)
    for ti in tracks_used:
        d_list = groups.get(ti, [])
        src = job_dir / tracks[ti]["path"]
        outf = out_dir_p(job_dir) / f"inter-{ti}.mp4"
        render_track(src, outf, d_list, duration,
                     progress_cb=lambda p, _ti=ti: progress_cb(
                         (_ti + p) / (len(tracks_used) + 1)) if progress_cb else None,
                     fps=fps)
        inter[ti] = outf

    ov_list = [d for d in decisions if d["type"] in
               ("text_overlay", "lower_third", "polaroid", "citation_card", "sticker_anim")
               and d.get("params", {}).get("_png")]
    fc_asm, asm_label = _assembly_graph(spans, inter, spans_meta)
    inputs: List[str] = []
    for ti in tracks_used:
        inputs += ["-i", str(inter[ti])]
    fi = len(tracks_used)
    fc = fc_asm
    last = asm_label
    for d in ov_list:
        inputs += ["-loop", "1", "-t", f"{max(d['dur'], 0.4):.3f}", "-i", d["params"]["_png"]]
        nxt = f"ov{fi}"
        dur = max(d["dur"], 0.4)
        fadeout = f"fade=t=out:st={dur - 0.35:.3f}:d=0.35:alpha=1," if dur > 0.8 else ""
        fc += (f";[{fi}:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1,{fadeout}"
               f"setpts=PTS-STARTPTS+{d['t']:.3f}/TB[oi{fi}]")
        p = d["params"]
        if d["type"] == "sticker_anim":
            x, y = _sticker_xy(p.get("path", "rtl"), d["t"], d["dur"])
        elif d["type"] in ("polaroid", "citation_card"):
            x, y = f"'{_overlay_pos(p.get('pos', 'right'))}'", f"'{_overlay_y_bottom()}'"
        else:
            x, y = "0", "0"
        fc += (f";[{last}][oi{fi}]overlay=x={x}:y={y}:"
               f"enable='between(t,{d['t']:.3f},{d['t'] + d['dur']:.3f})'[{nxt}]")
        last = nxt
        fi += 1
    fc += f";[{last}]format=yuv420p[vout]"

    # audio traccia 0 + narrazioni
    narrations = [d for d in decisions if d["type"] == "narration" and d.get("tts_clip")]
    for d in narrations:
        d["params"]["_tts_path"] = str(job_dir / d["tts_clip"])
    audio_idx = len(tracks_used) + len(ov_list)
    inputs += []
    fc_audio_parts: List[str] = []
    audio_label = None
    if tracks[0].get("has_audio"):
        inputs += ["-i", str(src0)]
        fc_audio_parts.append(f";[{audio_idx}:a]aresample=44100,"
                              f"aformat=sample_fmts=fltp:channel_layouts=stereo[ab0]")
        audio_label = "ab0"
    elif narrations:
        inputs += ["-f", "lavfi", "-t", f"{duration:.3f}",
                   "-i", "anullsrc=r=44100:cl=stereo"]
        audio_label = f"an{audio_idx}"
        fc_audio_parts.append(f";[{audio_idx}:a]aformat=sample_fmts=fltp:"
                              f"channel_layouts=stereo[{audio_label}]")
    if narrations:
        mix_parts, tts_inputs, _ = _narration_mix(narrations, audio_idx + 1)
        inputs += tts_inputs
        fc_audio_parts.append(";" + ";".join(mix_parts))
        mix_in = [audio_label] + [f"na{k}" for k in range(len(narrations))]
        audio_label = "afinal"
        fc_audio_parts.append(";" + "".join(f"[{l}]" for l in mix_in)
                              + f"amix=inputs={len(mix_in)}:normalize=0:duration=longest"
                                f"[{audio_label}]")
    if not audio_label:
        # nessuna sorgente audio: silenzio (input in fase input)
        fc_audio_parts.append(f";[{len(inputs)}:a]aformat=sample_fmts=fltp:"
                              f"channel_layouts=stereo[asilent]")
        inputs += ["-f", "lavfi", "-t", f"{duration:.3f}",
                   "-i", "anullsrc=r=44100:cl=stereo"]
        audio_label = "asilent"
    fc += "".join(fc_audio_parts)

    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[vout]",
            "-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-t", f"{duration:.3f}", "-r", str(fps)] + _encode_args() + [str(final)]
    _run_ffmpeg(cmd, progress_cb and (lambda p: progress_cb(0.9 + p * 0.1)), duration)
    return final


def out_dir_p(job_dir: Path) -> Path:
    d = job_dir / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d
