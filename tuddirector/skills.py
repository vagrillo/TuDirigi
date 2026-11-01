"""Skill di regia YAML: carimento, validazione, salvataggio, elenco scenari.

Una skill configura il "personaggio" del regista LLM per uno scenario specifico.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import SKILLS_DIR
from .director_tools import TOOL_IDS  # registro dei tool disponibili

log = logging.getLogger("tuddirector.skills")


class SkillError(Exception):
    pass


def _parse_yaml_text(text: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SkillError(f"YAML non valido: {e}")
    if not isinstance(data, dict):
        raise SkillError("la skill deve essere una mappa YAML")
    return data


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Valida e normalizza una skill; solleva SkillError se errata."""
    if not data.get("name"):
        raise SkillError("campo 'name' obbligatorio")
    if not data.get("description"):
        data["description"] = ""
    if not isinstance(data.get("system_extra") or "", str):
        raise SkillError("'system_extra' deve essere testo")
    tools = data.get("tools")
    if tools is None:
        data["tools"] = list(TOOL_IDS)
    else:
        if not isinstance(tools, list) or any(t not in TOOL_IDS for t in tools):
            bad = [t for t in (tools or []) if t not in TOOL_IDS]
            raise SkillError(f"tool non validi: {bad}. Disponibili: {sorted(TOOL_IDS)}")
        if not tools:
            raise SkillError("la whitelist 'tools' non può essere vuota")
    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise SkillError("'limits' deve essere una mappa")
    data["limits"] = {
        "max_simultaneous_overlays": int(limits.get("max_simultaneous_overlays", 2)),
        "max_zoom_strength": float(limits.get("max_zoom_strength", 0.45)),
        "max_decisions": int(limits.get("max_decisions", 8)),
    }
    style = data.get("style") or {}
    data["style"] = {
        "pacing": str(style.get("pacing", "dinamico")),
        "tone": str(style.get("tone", "neutro")),
    }
    preset = data.get("preset_effects") or {}
    if not isinstance(preset, dict):
        preset = {}
    data["preset_effects"] = {k: float(v) for k, v in preset.items()
                              if k in ("brightness", "saturation", "contrast")}
    data["scenario"] = str(data.get("scenario", "base"))
    return data


def skill_path(skill_id: str) -> Path:
    if not skill_id.replace("_", "").replace("-", "").isalnum():
        raise SkillError("id non valido")
    return SKILLS_DIR / f"{skill_id}.yaml"


def load_file(path: Path) -> Dict[str, Any]:
    data = _parse_yaml_text(path.read_text())
    data = validate(data)
    data["id"] = path.stem
    data["file"] = str(path)
    return data


def get(skill_id: str) -> Optional[Dict[str, Any]]:
    p = skill_path(skill_id)
    if not p.exists():
        return None
    try:
        return load_file(p)
    except SkillError as e:
        log.error("skill %s non valida: %s", skill_id, e)
        return None


def list_skills() -> List[Dict[str, Any]]:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(SKILLS_DIR.glob("*.yaml")):
        try:
            out.append(load_file(p))
        except SkillError as e:
            out.append({"id": p.stem, "name": p.stem, "invalid": True, "error": str(e),
                        "description": "", "file": str(p)})
    return out


def save(skill_id: str, yaml_text: str) -> Dict[str, Any]:
    """Valida e salva; ritorna la skill normalizzata."""
    data = _parse_yaml_text(yaml_text)
    data = validate(data)
    p = skill_path(skill_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml_text if yaml_text.endswith("\n") else yaml_text + "\n")
    data["id"] = skill_id
    data["file"] = str(p)
    return data


def delete(skill_id: str) -> bool:
    p = skill_path(skill_id)
    if p.exists():
        p.unlink()
        return True
    return False


def resolve(skill_id: Optional[str]) -> Dict[str, Any]:
    """Skill richiesta o fallback 'base'; mai None."""
    sk = get(skill_id or "base") or get("base")
    if sk is None:  # emergency inline default
        sk = validate({"name": "Regista Base", "description": "skill di emergenza",
                       "system_extra": "", "tools": list(TOOL_IDS)})
        sk["id"] = "base"
    return sk
