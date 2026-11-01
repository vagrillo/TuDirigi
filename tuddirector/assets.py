"""Asset finder per polaroid e citation_card.

Ordine (assets_policy=auto):
  1. Openverse API (senza chiave, licenze aperte)
  2. Wikimedia Commons (ideale per autori/libri/luoghi)
  3. fal.ai flux/schnell (generazione) — sempre se le ricerche falliscono
Ritorna un path locale .jpg.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .config import settings

log = logging.getLogger("tuddirector.assets")

_UA = {"User-Agent": "TuDirector/0.1 (self-hosted AI video editor)"}


class AssetError(Exception):
    pass


def _clean(query: str) -> str:
    q = re.sub(r"[^\w\sàèéìòù]", " ", query, flags=re.UNICODE)
    return " ".join(q.split())[:120]


def _dl(url: str, dest: Path, min_bytes: int = 8000) -> bool:
    try:
        r = requests.get(url, headers=_UA, timeout=20)
        if r.status_code != 200 or len(r.content) < min_bytes:
            return False
        dest.write_bytes(r.content)
        if not _is_meaningful_image(dest):
            return False  # immagine vuota/bianca/uniforme → prova la fonte successiva
        return True
    except requests.RequestException:
        return False


def _is_meaningful_image(path: Path) -> bool:
    """Scarta immagini quasi uniformi (bianche/nero/sfuocate): polaroid mai vuote."""
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(path).convert("L").resize((48, 32))
        a = np.asarray(img, dtype=np.float32)
        return float(a.std()) > 6.0 and 5.0 < float(a.mean()) < 250.0
    except Exception:
        return True  # non verificabile: accetta (il placeholder copre il resto)


# ---------------------------------------------------------------- Openverse
def openverse_search(query: str, dest: Path) -> bool:
    try:
        r = requests.get("https://api.openverse.org/v1/images/",
                         params={"q": _clean(query), "page_size": 8, "mature": "false",
                                 "license_type": "all-cc"},
                         headers=_UA, timeout=15)
        if r.status_code != 200:
            return False
        for item in r.json().get("results", []):
            url = item.get("url") or item.get("thumbnail")
            if url and _dl(url, dest):
                return True
    except requests.RequestException as e:
        log.debug("openverse errore: %s", e)
    return False


# ---------------------------------------------------------------- Wikimedia
def wikimedia_search(query: str, dest: Path) -> bool:
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php",
                         params={"action": "query", "format": "json",
                                 "generator": "search",
                                 "gsrsearch": _clean(query), "gsrlimit": "6",
                                 "gsrnamespace": "6",
                                 "prop": "imageinfo", "iiprop": "url|size",
                                 "iiurlwidth": "1024"},
                         headers=_UA, timeout=15)
        if r.status_code != 200:
            return False
        pages = (r.json().get("query") or {}).get("pages") or {}
        cand = sorted(pages.values(),
                      key=lambda p: -(p.get("imageinfo") or [{}])[0].get("width", 0))
        for p in cand:
            info = (p.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            if not url or not url.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if url.lower().endswith((".svg", ".gif", ".tif", ".tiff", ".webp")):
                continue
            if _dl(url, dest):
                return True
    except requests.RequestException as e:
        log.debug("wikimedia errore: %s", e)
    return False


# ---------------------------------------------------------------- fal.ai generazione
def fal_generate(prompt: str, dest: Path) -> bool:
    s = settings()
    if not s.fal_key:
        return False
    try:
        import base64
        r = requests.post(
            f"{s.fal_base}/{s.fal_image_model}",
            headers={"Authorization": f"Key {s.fal_key}", "Content-Type": "application/json"},
            json={"prompt": f"photograph of {prompt}, realistic, editorial photo",
                  "image_size": "square_hd", "num_images": 1},
            timeout=120)
        if r.status_code != 200:
            return False
        data = r.json()
        imgs = data.get("images") or []
        if not imgs:
            return False
        url = imgs[0].get("url")
        if not url:
            return False
        ir = requests.get(url, timeout=60)
        if ir.status_code != 200 or len(ir.content) < 2000:
            return False
        dest.write_bytes(ir.content)
        return True
    except requests.RequestException as e:
        log.debug("fal generate errore: %s", e)
        return False


# ---------------------------------------------------------------- placeholder
def placeholder_image(query: str, dest: Path) -> bool:
    """Immagine placeholder estetica con Pillow (mock mode)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import hashlib
        h = hashlib.md5(query.encode()).hexdigest()
        hue = int(h[:2], 16) * 360 / 255
        import colorsys
        rgb = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue / 360, 0.45, 0.75))
        img = Image.new("RGB", (1024, 1024), rgb)
        d = ImageDraw.Draw(img)
        for i in range(0, 1024, 64):
            shade = tuple(max(0, c - i % 128) for c in rgb)
            d.line([(0, i), (1024, i - 200)], fill=shade, width=8)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        except Exception:
            font = ImageFont.load_default()
        d.rectangle([60, 440, 964, 580], fill=(0, 0, 0))
        d.text((90, 480), _clean(query)[:28], fill=(255, 255, 255), font=font)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, quality=88)
        return True
    except Exception as e:
        log.error("placeholder fallito: %s", e)
        return False


# ---------------------------------------------------------------- API
def find_image(query: str, out_path: Path, kind: str = "fact",
               policy: Optional[str] = None) -> Optional[Path]:
    """Trova/genera un'immagine per la query; mai lancia eccezioni.

    kind: 'fact' (polaroid), 'author', 'book'
    """
    s = settings()
    policy = policy or s.assets_policy
    out_path.parent.mkdir(parents=True, exist_ok=True)
    q = _clean(query) or "immagine"
    if kind in ("author", "book"):
        attempts = ["wikimedia", "openverse", "placeholder"]
    else:
        attempts = ["openverse", "wikimedia", "placeholder"]
    if policy == "generate":
        attempts = ["generate", "placeholder"]
    elif policy == "search":
        attempts = [a for a in attempts if a != "generate"]

    for method in attempts:
        if method == "openverse":
            ok = openverse_search(q, out_path)
        elif method == "wikimedia":
            ok = wikimedia_search(q, out_path)
        elif method == "generate":
            ok = fal_generate(q, out_path)
        else:
            ok = placeholder_image(q, out_path)
        if ok:
            log.info("asset ok via %s per '%s'", method, q)
            return out_path
        time.sleep(0.3)
    return None


def resolve_assets_for_decisions(decisions: list, job_dir: Path,
                                 progress_cb=None) -> dict:
    """Per ogni decisione che richiede immagini scarica/genera l'asset.

    Modifica in place: aggiunge decision['asset'] = path relativo al job.
    Ritorna {decision_index: path}.
    """
    out = {}
    needing = [(i, d) for i, d in enumerate(decisions)
               if d.get("type") in ("polaroid", "citation_card")]
    for n, (i, d) in enumerate(needing):
        p = d.get("params", {})
        query = p.get("query") or p.get("caption") or ""
        kind = "author" if (d.get("type") == "citation_card"
                            and p.get("kind") == "author") else \
               "book" if d.get("type") == "citation_card" else "fact"
        fname = f"{d.get('type')}-{n}.jpg"
        dest = job_dir / "assets" / fname
        path = find_image(query, dest, kind=kind)
        if path:
            d["asset"] = f"assets/{fname}"
            out[i] = d["asset"]
        if progress_cb:
            progress_cb((n + 1) / max(1, len(needing)))
    return out
