"""Load config.yaml and resolve template / placeholder values."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml

CONFIG_PATH = Path(os.getenv("CONTRACT_GENERATOR_CONFIG_PATH", "/app/config.yaml"))
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
FIELD_REF_RE = re.compile(r"^\{([^{}]+)\}$")


class ConfigError(Exception):
    """Raised when config is missing or invalid."""


class TemplateResolveError(Exception):
    """Raised when a template cannot be resolved from the request."""

    def __init__(self, message: str, error_code: str = "TEMPLATE_RESOLVE_ERROR"):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass
class RateLimitConfig:
    read_qps: float = 4.0
    write_qps: float = 3.0
    copy_retry_max: int = 3
    copy_retry_delay_ms: int = 800
    batch_size: int = 20
    batch_delay_ms: int = 350


@dataclass
class ResolvedTemplate:
    key: str
    template_token: str
    folder_token: str
    document_name_pattern: str
    party_b_full_name: str = ""
    placeholders: Dict[str, str] = field(default_factory=dict)
    required_placeholders: List[str] = field(default_factory=list)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    sheet: Optional[Dict[str, Any]] = None
    explicit: bool = False


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(match: re.Match) -> str:
            return os.getenv(match.group(1), "")

        return ENV_VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Config not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")

    return _expand_env(data)


def parse_rate_limit(raw: Optional[Dict[str, Any]]) -> RateLimitConfig:
    raw = raw or {}
    return RateLimitConfig(
        read_qps=float(raw.get("read_qps", 4.0)),
        write_qps=float(raw.get("write_qps", 3.0)),
        copy_retry_max=int(raw.get("copy_retry_max", 3)),
        copy_retry_delay_ms=int(raw.get("copy_retry_delay_ms", 800)),
        batch_size=int(raw.get("batch_size", 20)),
        batch_delay_ms=int(raw.get("batch_delay_ms", 350)),
    )


def list_templates(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    config = config or load_config()
    templates = config.get("templates") or {}
    result = []
    for key, tpl in templates.items():
        if not isinstance(tpl, dict):
            continue
        result.append(
            {
                "key": key,
                "template_token": tpl.get("template_token") or "",
                "folder_token": tpl.get("folder_token") or "",
                "document_name_pattern": tpl.get("document_name_pattern") or "",
                "party_b_full_name": tpl.get("party_b_full_name") or "",
                "placeholder_count": len(tpl.get("placeholders") or {}),
            }
        )
    return result


def resolve_template(
    *,
    signing_unit: Optional[str] = None,
    template_token: Optional[str] = None,
    folder_token: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> ResolvedTemplate:
    """Resolve template from explicit tokens or signing_unit mapping."""
    config = config or load_config()
    defaults = config.get("defaults") or {}
    rate_limit = parse_rate_limit(defaults.get("rate_limit"))
    required = list(defaults.get("required_placeholders") or [])

    explicit_token = (template_token or "").strip()
    if explicit_token:
        folder = (folder_token or "").strip() or str(defaults.get("folder_token") or "").strip()
        if not folder:
            raise TemplateResolveError(
                "folder_token is required when using explicit template_token",
                error_code="MISSING_FOLDER_TOKEN",
            )
        return ResolvedTemplate(
            key="__explicit__",
            template_token=explicit_token,
            folder_token=folder,
            document_name_pattern=str(defaults.get("document_name_pattern") or "合同-{订单号}"),
            placeholders={},
            required_placeholders=required,
            rate_limit=rate_limit,
            explicit=True,
        )

    unit = (signing_unit or "").strip()
    if not unit:
        # Fall back to defaults template_token for POC
        default_token = str(defaults.get("template_token") or "").strip()
        default_folder = str(defaults.get("folder_token") or "").strip()
        if default_token and default_folder:
            return ResolvedTemplate(
                key="__defaults__",
                template_token=default_token,
                folder_token=default_folder,
                document_name_pattern=str(defaults.get("document_name_pattern") or "合同-{订单号}"),
                placeholders={},
                required_placeholders=required,
                rate_limit=rate_limit,
                explicit=True,
            )
        raise TemplateResolveError(
            "signing_unit or template_token is required",
            error_code="MISSING_TEMPLATE_SELECTOR",
        )

    unit_map = config.get("signing_unit_map") or {}
    template_key = unit_map.get(unit, unit)
    templates = config.get("templates") or {}
    tpl = templates.get(template_key)
    if not isinstance(tpl, dict):
        raise TemplateResolveError(
            f"No template configured for signing_unit={unit!r} (key={template_key!r})",
            error_code="UNKNOWN_SIGNING_UNIT",
        )

    token = str(tpl.get("template_token") or defaults.get("template_token") or "").strip()
    folder = str(tpl.get("folder_token") or defaults.get("folder_token") or "").strip()
    if not token:
        raise TemplateResolveError(
            f"template_token missing for template key={template_key!r}",
            error_code="MISSING_TEMPLATE_TOKEN",
        )
    if not folder:
        raise TemplateResolveError(
            f"folder_token missing for template key={template_key!r}",
            error_code="MISSING_FOLDER_TOKEN",
        )

    tpl_required = tpl.get("required_placeholders")
    if isinstance(tpl_required, list) and tpl_required:
        required = [str(x) for x in tpl_required]

    sheet_cfg = tpl.get("sheet")
    if sheet_cfg is not None and not isinstance(sheet_cfg, dict):
        raise TemplateResolveError(
            f"sheet config must be a mapping for template key={template_key!r}",
            error_code="INVALID_SHEET_CONFIG",
        )

    return ResolvedTemplate(
        key=str(template_key),
        template_token=token,
        folder_token=folder,
        document_name_pattern=str(
            tpl.get("document_name_pattern")
            or defaults.get("document_name_pattern")
            or "合同-{订单号}"
        ),
        party_b_full_name=str(tpl.get("party_b_full_name") or ""),
        placeholders=dict(tpl.get("placeholders") or {}),
        required_placeholders=required,
        rate_limit=rate_limit,
        sheet=dict(sheet_cfg) if isinstance(sheet_cfg, dict) else None,
        explicit=False,
    )


def format_currency(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).replace(",", "").replace("，", "").strip()
    try:
        return f"{float(text):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def format_people_count(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip()
    text = re.sub(r"人$", "", text)
    m = re.search(r"\d+", text)
    return m.group(0) if m else text


def format_date_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts // 1000
        dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
        return dt.strftime("%Y/%m/%d")
    text = str(value).strip()
    # Already formatted
    if re.match(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}", text):
        return text.replace("-", "/")
    return text


def format_today() -> str:
    return datetime.now(tz=LOCAL_TZ).strftime("%Y年%m月%d日")


def _lookup_field(fields: Dict[str, Any], field_name: str) -> Any:
    if field_name in fields:
        return fields[field_name]
    # Tolerate whitespace differences
    for key, value in fields.items():
        if str(key).strip() == field_name:
            return value
    return None


def _resolve_mapping_value(
    mapping: str,
    fields: Dict[str, Any],
    party_b_full_name: str,
) -> str:
    mapping = str(mapping or "").strip()
    if not mapping:
        return ""

    if mapping == "@today":
        return format_today()
    if mapping == "@party_b_full_name":
        return party_b_full_name or ""

    m = FIELD_REF_RE.match(mapping)
    if m:
        field_name = m.group(1).strip()
        raw = _lookup_field(fields, field_name)
        if field_name in {"合同价款"}:
            return format_currency(raw)
        if field_name in {"执行日期", "活动日期"}:
            return format_date_value(raw)
        if field_name in {"执行人数", "活动人数"}:
            return format_people_count(raw)
        if raw is None:
            return ""
        return str(raw).strip()

    # Literal
    return mapping


def build_placeholder_values(
    tpl: ResolvedTemplate,
    fields: Optional[Dict[str, Any]] = None,
    placeholders: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build final {{key}} -> value map.

    Explicit `placeholders` override config-mapped values.
    """
    fields = fields or {}
    values: Dict[str, str] = {}

    for key, mapping in (tpl.placeholders or {}).items():
        values[key] = _resolve_mapping_value(mapping, fields, tpl.party_b_full_name)

    # Common auto fields when using fields without full placeholder map
    if fields and not tpl.placeholders:
        auto_map = {
            "甲方名称": "{单位}",
            "甲方联系人": "{联系人}",
            "甲方电话": "{联系方式}",
            "合同价款": "{合同价款}",
            "活动日期": "{执行日期}",
            "活动人数": "{执行人数}",
            "活动描述": "{客户需求}",
            "订单号": "{订单号}",
            "策划师": "{策划师}",
            "签订日期": "@today",
            "乙方名称": "@party_b_full_name",
        }
        for key, mapping in auto_map.items():
            if key not in values or not values[key]:
                values[key] = _resolve_mapping_value(mapping, fields, tpl.party_b_full_name)

    for key, value in (placeholders or {}).items():
        if value is None:
            continue
        values[str(key)] = str(value)

    return values


def format_document_name(
    pattern: str,
    fields: Optional[Dict[str, Any]] = None,
    document_name: Optional[str] = None,
) -> str:
    if document_name and str(document_name).strip():
        return str(document_name).strip()

    fields = fields or {}
    name = str(pattern or "合同")

    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        raw = _lookup_field(fields, key)
        return "" if raw is None else str(raw).strip()

    name = re.sub(r"\{([^{}]+)\}", repl, name)
    name = name.strip() or "合同"
    # Feishu name max 256 bytes
    encoded = name.encode("utf-8")
    if len(encoded) > 256:
        while len(name.encode("utf-8")) > 250:
            name = name[:-1]
        name = name + "…"
    return name


def missing_required(values: Dict[str, str], required: List[str]) -> List[str]:
    return [k for k in required if not str(values.get(k) or "").strip()]
