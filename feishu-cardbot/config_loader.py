"""加载 app.yaml + skills/*.yaml，展平为卡片引擎运行时配置。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml


class ConfigError(Exception):
    """配置缺失或格式错误。"""


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            return os.getenv(m.group(1), "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    return _expand_env(data)


def resolve_config_dir(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return explicit
    env = os.getenv("FEISHU_CARDBOT_CONFIG_DIR") or os.getenv("CARDBOT_CONFIG_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidate = here / "config"
    return candidate if candidate.is_dir() else here


def load_app(config_dir: Path) -> Dict[str, Any]:
    return _read_yaml(config_dir / "app.yaml")


def load_skills(config_dir: Path) -> Dict[str, Dict[str, Any]]:
    skills_dir = config_dir / "skills"
    if not skills_dir.is_dir():
        raise ConfigError(f"skills directory not found: {skills_dir}")
    skills: Dict[str, Dict[str, Any]] = {}
    for path in sorted(skills_dir.glob("*.yaml")):
        data = _read_yaml(path)
        sid = str(data.get("id") or path.stem)
        data["id"] = sid
        if data.get("enabled", True) is False:
            continue
        action = str(data.get("action") or "upsert_record")
        data["action"] = action
        if action == "search":
            continue
        target = data.get("target") or {}
        if not data.get("fields"):
            raise ConfigError(f"skill {sid} missing fields")
        if not target.get("table_id"):
            raise ConfigError(f"skill {sid} missing target.table_id")
        skills[sid] = data
    if not skills:
        raise ConfigError("no enabled upsert skills found")
    return skills


def skill_to_runtime(skill: Dict[str, Any], app: Dict[str, Any]) -> Dict[str, Any]:
    target = skill.get("target") or {}
    groups = skill.get("require_any_group") or skill.get("payment_groups") or []
    attachment = skill.get("attachment") or {}
    attach_key = attachment.get("field_key")
    if not attach_key:
        for key, cfg in (skill.get("fields") or {}).items():
            if (cfg or {}).get("type") == "attachment":
                attach_key = key
                break
    base_id = target.get("base_id") or os.getenv("FEISHU_BASE_ID", "")
    return {
        "skill_id": skill["id"],
        "skill_name": skill.get("name") or skill["id"],
        "action": str(skill.get("action") or "upsert_record"),
        "base_id": base_id,
        "table_id": target.get("table_id"),
        "session_ttl_minutes": app.get("session_ttl_minutes", 30),
        "menu_event_key": str(app.get("menu_event_key") or "add_supplier"),
        "fields": skill.get("fields") or {},
        "require_any_group": groups,
        "payment_groups": groups,
        "prompts": skill.get("prompts") or {},
        "dedupe": skill.get("dedupe") or {},
        "rules": skill.get("rules") or [],
        "permissions": {
            **(app.get("permissions") or {}),
            **(skill.get("permissions") or {}),
        },
        "attachment_field": attach_key or "",
        "idle_help": app.get("idle_help") or "",
    }


def load_platform(
    config_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    root = resolve_config_dir(config_dir)
    app = load_app(root)
    skills = load_skills(root)
    return app, skills


def load_supplier_runtime(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    app, skills = load_platform(config_dir)
    skill = skills.get("supplier") or next(iter(skills.values()))
    return skill_to_runtime(skill, app)
