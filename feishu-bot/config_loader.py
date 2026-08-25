"""加载 app.yaml + skills/*.yaml，并把 skill 展平为 Form Engine 运行时配置。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class ConfigError(Exception):
    """配置缺失或格式错误。"""

    pass


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """递归展开 YAML 中的 ${ENV_VAR}。"""
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
    """解析配置根目录：显式参数 → 环境变量 → 包旁 ./config。"""
    if explicit is not None:
        return explicit
    env = (
        os.getenv("FEISHU_BOT_CONFIG_DIR")
        or os.getenv("SUPPLIER_BOT_CONFIG_DIR")
        or os.getenv("CHAT_BOT_CONFIG_DIR")
    )
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidate = here / "config"
    if candidate.is_dir():
        return candidate
    return here


def load_app(config_dir: Path) -> Dict[str, Any]:
    """读取全局 app.yaml。"""
    return _read_yaml(config_dir / "app.yaml")


def load_skills(config_dir: Path) -> Dict[str, Dict[str, Any]]:
    """加载 skills/*.yaml；enabled:false 跳过。

    action=search：只读查询，不要求 fields/table_id。
    默认（录入）：必须有 fields 与 target.table_id。
    """
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
        target = data.get("target") or {}
        if action == "search":
            skills[sid] = data
            continue
        if not data.get("fields"):
            raise ConfigError(f"skill {sid} missing fields")
        if not target.get("table_id"):
            raise ConfigError(f"skill {sid} missing target.table_id")
        skills[sid] = data
    if not skills:
        raise ConfigError("no enabled skills found")
    return skills


def _merge_commands(app: Dict[str, Any], skill: Dict[str, Any]) -> Dict[str, List[str]]:
    """合并全局与 skill 级口令（确认/取消/跳过/覆盖）；skill 优先。"""
    base = dict(app.get("commands") or {})
    override = skill.get("commands") or {}
    legacy_map = {
        "cancel": "cancel_words",
        "confirm": "confirm_words",
        "skip": "skip_words",
        "overwrite": "overwrite_words",
    }
    out: Dict[str, List[str]] = {}
    for key in ("cancel", "confirm", "skip", "overwrite"):
        if key in override and override[key]:
            out[key] = list(override[key])
        elif legacy_map[key] in skill:
            out[key] = list(skill[legacy_map[key]])
        else:
            out[key] = list(base.get(key) or [])
    return out


def skill_to_runtime(skill: Dict[str, Any], app: Dict[str, Any]) -> Dict[str, Any]:
    """把 skill + app 展平为 dialog/extractor 使用的 runtime dict。

    含：base_id/table_id、字段、prompts、dedupe、rules、permissions、ai 等。
    """
    target = skill.get("target") or {}
    commands = _merge_commands(app, skill)
    groups = skill.get("require_any_group") or skill.get("payment_groups") or []
    attachment = skill.get("attachment") or {}
    attach_key = attachment.get("field_key")
    if not attach_key:
        for key, cfg in (skill.get("fields") or {}).items():
            if (cfg or {}).get("type") == "attachment":
                attach_key = key
                break

    base_id = target.get("base_id") or os.getenv("FEISHU_BASE_ID", "")
    runtime: Dict[str, Any] = {
        "skill_id": skill["id"],
        "skill_name": skill.get("name") or skill["id"],
        "action": str(skill.get("action") or "upsert_record"),
        "base_id": base_id,
        "table_id": target.get("table_id"),
        "webhook_url": target.get("webhook_url") or os.getenv("N8N_QUERY_WEBHOOK_URL", ""),
        "session_ttl_minutes": app.get("session_ttl_minutes", 30),
        "triggers": list(skill.get("triggers") or []),
        "cancel_words": commands["cancel"],
        "confirm_words": commands["confirm"],
        "skip_words": commands["skip"],
        "overwrite_words": commands["overwrite"],
        "fields": skill.get("fields") or {},
        "require_any_group": groups,
        "payment_groups": groups,  # validator 旧别名
        "aliases": skill.get("aliases") or {},
        "prompts": skill.get("prompts") or {},
        "dedupe": skill.get("dedupe") or {},
        "rules": skill.get("rules") or [],
        "ai": {
            **(app.get("ai") or {}),
            **(skill.get("ai") or {}),
        },
        "permissions": {
            **(app.get("permissions") or {}),
            **(skill.get("permissions") or {}),
        },
        "attachment_field": attach_key or "",
        "reply": skill.get("reply")
        or {"mode": (app.get("channels") or {}).get("reply_mode") or "card"},
        "idle_help": app.get("idle_help") or "",
        "ambiguous_reply": (app.get("router") or {}).get("ambiguous_reply") or "",
    }
    return runtime


def load_platform(
    config_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """返回 (app 配置, skills_by_id)。兼容旧版单文件 config.yaml。"""
    root = resolve_config_dir(config_dir)
    legacy = root / "config.yaml" if root.name != "config" else root.parent / "config.yaml"
    if not (root / "app.yaml").exists() and legacy.exists():
        # 旧扁平配置包装成单一 supplier skill
        flat = _read_yaml(legacy)
        app = {
            "session_ttl_minutes": flat.get("session_ttl_minutes", 30),
            "commands": {
                "cancel": flat.get("cancel_words") or [],
                "confirm": flat.get("confirm_words") or [],
                "skip": flat.get("skip_words") or [],
                "overwrite": flat.get("overwrite_words") or [],
            },
            "router": {"strategy": "trigger_first"},
            "idle_help": "发送「添加供应商」开始录入。",
        }
        skill = {
            "id": "supplier",
            "name": "供应商录入",
            "enabled": True,
            "triggers": flat.get("triggers") or [],
            "fields": flat.get("fields") or {},
            "aliases": flat.get("aliases") or {},
            "require_any_group": flat.get("payment_groups")
            or flat.get("require_any_group")
            or [],
            "target": {
                "type": "bitable",
                "base_id": flat.get("base_id") or os.getenv("FEISHU_BASE_ID", ""),
                "table_id": flat.get("table_id"),
            },
            "dedupe": {
                "enabled": True,
                "match_field_key": "supplier_name",
                "on_hit": "ask_overwrite",
            },
            "attachment": {"field_key": "qrcode"},
            "prompts": {},
            "commands": {
                "cancel": flat.get("cancel_words") or [],
                "confirm": flat.get("confirm_words") or [],
                "skip": flat.get("skip_words") or [],
                "overwrite": flat.get("overwrite_words") or [],
            },
        }
        return app, {"supplier": skill}

    app = load_app(root)
    skills = load_skills(root)
    return app, skills


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """兼容入口：返回默认（supplier）skill 的 runtime。

    多 skill 场景请用 load_platform() + skill_to_runtime()。
    """
    if path is not None and path.is_file():
        flat = _read_yaml(path)
        app, skills = load_platform(
            path.parent / "config" if (path.parent / "config").is_dir() else path.parent
        )
        if (path.parent / "config" / "app.yaml").exists():
            app, skills = load_platform(path.parent / "config")
            skill = skills.get("supplier") or next(iter(skills.values()))
            return skill_to_runtime(skill, app)
        app = {
            "session_ttl_minutes": flat.get("session_ttl_minutes", 30),
            "commands": {},
            "router": {},
            "idle_help": "",
        }
        skill = {
            "id": "supplier",
            "name": "供应商录入",
            "triggers": flat.get("triggers") or [],
            "fields": flat.get("fields") or {},
            "aliases": flat.get("aliases") or {},
            "require_any_group": flat.get("payment_groups") or [],
            "target": {
                "base_id": flat.get("base_id") or "",
                "table_id": flat.get("table_id"),
            },
            "commands": {
                "cancel": flat.get("cancel_words") or [],
                "confirm": flat.get("confirm_words") or [],
                "skip": flat.get("skip_words") or [],
                "overwrite": flat.get("overwrite_words") or [],
            },
            "dedupe": {
                "enabled": True,
                "match_field_key": "supplier_name",
                "on_hit": "ask_overwrite",
            },
            "attachment": {"field_key": "qrcode"},
            "prompts": {},
        }
        return skill_to_runtime(skill, app)

    app, skills = load_platform(path if path and path.is_dir() else None)
    skill = skills.get("supplier") or next(iter(skills.values()))
    return skill_to_runtime(skill, app)
