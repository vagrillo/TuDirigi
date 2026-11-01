"""Composizione overlay: SVG→PNG (cairosvg), polaroid (Pillow), sticker, lower third.

Griglia di design 800×480 (stessa dei frame); render finale scala a out_w×out_h.
Tutti gli output sono PNG RGBA con trasparenza.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import settings

log = logging.getLogger("tuddirector.overlay")

DESIGN_W, DESIGN_H = 800, 480
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_ITALIC = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"

_BAD_SVG = re.compile(r"<\s*(script|foreignObject|iframe|object|embed|animate|use)\b", re.I)


def scale() -> float:
    s = settings()
    return s.out_w / DESIGN_W


def sanitize_svg(svg: str) -> str:
    """Pulisce l'SVG: niente script/foreignObject, root con viewBox corretto."""
    if _BAD_SVG.search(svg or ""):
        raise ValueError("SVG contiene elementi non consentiti")
    s = svg.strip()
    if "viewBox" not in s:
        s = s.replace("<svg", f'<svg viewBox="0 0 {DESIGN_W} {DESIGN_H}"', 1)
    # dimensione di rendering gestita da cairosvg (output_width)
    s = re.sub(r'(<svg[^>]*?)\swidth="[^"]*"', r"\1", s, count=1)
    s = re.sub(r'(<svg[^>]*?)\sheight="[^"]*"', r"\1", s, count=1)
    return s


def svg_to_png(svg: str, out_png: Path) -> Path:
    """Rasterizza un SVG (viewBox 800×480) in PNG RGBA alla risoluzione di output."""
    import cairosvg
    s = settings()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    clean = sanitize_svg(svg)
    cairosvg.svg2png(bytestring=clean.encode(), write_to=str(out_png),
                     output_width=s.out_w, output_height=s.out_h, background_color=None)
    return out_png


def fallback_title_svg(title: str, subtitle: str = "", style: str = "title") -> str:
    """Template SVG di riserva per overlay testuali."""
    def esc(t: str) -> str:
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    title = esc(title[:60]) or "Titolo"
    sub = esc(subtitle[:80])
    if style == "lower_third":
        y_title, y_sub = 386, 428
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {DESIGN_W} {DESIGN_H}">
  <rect x="40" y="{y_title-52}" width="620" height="{66 if not sub else 108}" rx="10" fill="#0b0e14" opacity="0.82"/>
  <rect x="40" y="{y_title-52}" width="10" height="{66 if not sub else 108}" rx="4" fill="#ff4d5a"/>
  <text x="70" y="{y_title}" font-family="DejaVu Sans" font-size="40" font-weight="bold" fill="#ffffff">{title}</text>
  {f'<text x="70" y="{y_sub}" font-family="DejaVu Sans" font-size="28" fill="#cfd8e3">{sub}</text>' if sub else ''}
</svg>'''
    # title centered
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {DESIGN_W} {DESIGN_H}">
  <rect x="110" y="180" width="580" height="120" rx="16" fill="#000000" opacity="0.72"/>
  <text x="400" y="235" text-anchor="middle" font-family="DejaVu Sans" font-size="46" font-weight="bold" fill="#ffffff">{title}</text>
  {f'<text x="400" y="278" text-anchor="middle" font-family="DejaVu Sans" font-size="26" fill="#dfe7f0">{sub}</text>' if sub else ''}
</svg>'''


# ---------------------------------------------------------------- polaroid
def make_polaroid(image_path: Path, caption: str, rotate: float, out_png: Path) -> Path:
    """Compone una polaroid: cornice bianca, foto, didascalia, rotazione + ombra."""
    s = settings()
    sc = scale()
    pw, ph = int(300 * sc), int(330 * sc)      # polaroid totale
    pad, cap_h = int(16 * sc), int(64 * sc)
    iw, ih = pw - 2 * pad, ph - 2 * pad - cap_h

    img = Image.open(image_path).convert("RGB")
    img = _cover(img, iw, ih)

    pol = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(pol)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=int(6 * sc), fill=(255, 255, 255, 242))
    pol.paste(img, (pad, pad))
    try:
        f = ImageFont.truetype(_FONT_ITALIC, int(22 * sc))
    except Exception:
        f = ImageFont.load_default()
    if caption:
        d.text((pw // 2, ph - cap_h // 2 - int(4 * sc)), caption[:40], font=f,
               fill=(40, 44, 52, 255), anchor="mm")

    # ombra
    shadow = Image.new("RGBA", (pw + 60, ph + 60), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([30, 30, pw + 30, ph + 30], radius=int(8 * sc), fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10 * sc))

    pol = pol.rotate(rotate, expand=True, resample=Image.BICUBIC)
    shadow = shadow.rotate(rotate, expand=True, resample=Image.BICUBIC)

    canvas = Image.new("RGBA", (shadow.width, shadow.height), (0, 0, 0, 0))
    canvas.alpha_composite(shadow, (0, 0))
    canvas.alpha_composite(pol, (0, 0))

    # riporta su canvas design (posizionamento gestito poi dal render)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return out_png


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Ritaglia/copre tipo CSS object-fit: cover."""
    iw, ih = img.size
    r = max(w / iw, h / ih)
    img = img.resize((int(iw * r) + 1, int(ih * r) + 1), Image.LANCZOS)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


# ---------------------------------------------------------------- sticker
_CAR_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 160">
  <g transform="rotate(-2 160 80)">
    <path d="M20 118 C 30 84, 60 66, 120 62 L 160 30 C 200 26, 250 44, 272 66
             C 292 72, 304 90, 304 110 L 300 122 L 24 122 Z" fill="#e74c3c" stroke="#8e2620" stroke-width="4"/>
    <path d="M126 64 L 158 38 C 192 36, 232 50, 250 64 Z" fill="#aed6f1" stroke="#8e2620" stroke-width="3"/>
    <circle cx="84" cy="124" r="26" fill="#2c3e50" stroke="#17202a" stroke-width="4"/>
    <circle cx="246" cy="124" r="26" fill="#2c3e50" stroke="#17202a" stroke-width="4"/>
    <circle cx="84" cy="124" r="10" fill="#bdc3c7"/>
    <circle cx="246" cy="124" r="10" fill="#bdc3c7"/>
    <circle cx="296" cy="96" r="7" fill="#fff3b0"/>
  </g>
</svg>'''

_ARROW_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 120">
  <path d="M10 60 L 210 60 M 210 60 L 160 24 M 210 60 L 160 96"
        stroke="#ffd54a" stroke-width="26" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
</svg>'''

_STAR_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <path d="M100 8 L124 76 L196 76 L138 118 L160 188 L100 146 L40 188 L62 118 L4 76 L76 76 Z"
        fill="#ffd54a" stroke="#c8a020" stroke-width="6" stroke-linejoin="round"/>
</svg>'''


def make_sticker(kind: str, out_png: Path, target_h: Optional[int] = None) -> Path:
    """PNG RGBA per sticker_anim (car, arrow, star)."""
    import cairosvg
    s = settings()
    th = target_h or int(s.out_h * 0.2)
    svg = {"car": _CAR_SVG, "arrow": _ARROW_SVG, "star": _STAR_SVG}.get(kind, _ARROW_SVG)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(out_png),
                     output_height=th, background_color=None)
    return out_png


# ---------------------------------------------------------------- prep per render
def prepare_overlays(decisions: list, job_dir: Path) -> dict:
    """Converte le decisioni grafiche in PNG pronti per il render.

    Aggiunge a ogni decisione: params['_png'] (path assoluto) e _w/_h px.
    """
    s = settings()
    overlays_dir = job_dir / "overlays"
    for i, d in enumerate(decisions):
        p = d.get("params", {})
        try:
            if d["type"] == "text_overlay":
                svg = p.get("svg") or ""
                png = overlays_dir / f"text-{i:03d}.png"
                try:
                    svg_to_png(svg, png)
                except Exception:
                    svg_to_png(fallback_title_svg("Titolo"), png)
                p["_png"] = str(png)
            elif d["type"] == "lower_third":
                png = overlays_dir / f"lt-{i:03d}.png"
                svg_to_png(fallback_title_svg(p.get("title", "Titolo"),
                                              p.get("subtitle", ""), style="lower_third"), png)
                p["_png"] = str(png)
            elif d["type"] == "polaroid":
                asset = d.get("asset")
                if not asset:
                    continue
                png = overlays_dir / f"pol-{i:03d}.png"
                make_polaroid(job_dir / asset, p.get("caption", ""),
                              float(p.get("rotate", -6)), png)
                p["_png"] = str(png)
            elif d["type"] == "citation_card":
                asset = d.get("asset")
                if not asset:
                    continue
                png = overlays_dir / f"cit-{i:03d}.png"
                kind = p.get("kind", "book")
                label = "AUTORE" if kind == "author" else "LIBRO"
                make_polaroid(job_dir / asset, f"{label}: {p.get('query', '')[:30]}",
                              float(p.get("rotate", 5 if kind == "book" else -5)), png)
                p["_png"] = str(png)
            elif d["type"] == "sticker_anim":
                png = overlays_dir / f"stk-{i:03d}.png"
                make_sticker(p.get("kind", "arrow"), png,
                             target_h=int(s.out_h * float(p.get("size", 0.2))))
                p["_png"] = str(png)
        except Exception as e:
            log.warning("overlay %s (#%d) fallito: %s", d["type"], i, e)
    return decisions
