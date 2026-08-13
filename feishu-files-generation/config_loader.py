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

CONFIG_PATH = Path(os.getenv("FEISHU_FILES_GENERATION_CONFIG_PATH", "/app/config.yaml"))
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
    # "docx" (default, contract / departure) or "spreadsheet" (standalone sheet)
    template_type: str = "docx"
    # Append fee/itinerary as embedded Sheet blocks after placeholder fill (contracts)
    append_detail_sheets: bool = False
    detail_sheets: Optional[Dict[str, Any]] = None
    # Alias key from output_folders when resolved via output_folder (may be empty)
    output_folder: str = ""
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


def list_output_folders(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Return alias -> folder_token map from config.output_folders."""
    config = config or load_config()
    raw = config.get("output_folders") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).strip(): str(v or "").strip()
        for k, v in raw.items()
        if str(k).strip() and str(v or "").strip()
    }


def lookup_output_folder_alias(
    alias: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve output_folders alias to token. Raises UNKNOWN_OUTPUT_FOLDER if missing."""
    key = str(alias or "").strip()
    if not key:
        return ""
    folders = list_output_folders(config)
    token = folders.get(key, "")
    if not token:
        raise TemplateResolveError(
            f"Unknown output_folder alias={key!r}; "
            f"known={sorted(folders.keys())}",
            error_code="UNKNOWN_OUTPUT_FOLDER",
        )
    return token


def resolve_folder_token(
    *,
    request_folder_token: Optional[str] = None,
    request_output_folder: Optional[str] = None,
    template_folder_token: Optional[str] = None,
    template_output_folder: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Resolve final folder_token and the alias used (if any).

    Priority (high → low):
      1. request.folder_token (raw)
      2. request.output_folder (alias → output_folders)
      3. template.output_folder (alias) then template.folder_token
      4. defaults.output_folder / defaults.folder_token
    """
    config = config or load_config()
    defaults = config.get("defaults") or {}

    raw = str(request_folder_token or "").strip()
    if raw:
        return raw, ""

    req_alias = str(request_output_folder or "").strip()
    if req_alias:
        return lookup_output_folder_alias(req_alias, config), req_alias

    tpl_alias = str(template_output_folder or "").strip()
    if tpl_alias:
        return lookup_output_folder_alias(tpl_alias, config), tpl_alias

    tpl_raw = str(template_folder_token or "").strip()
    if tpl_raw:
        return tpl_raw, ""

    defaults_alias = str(defaults.get("output_folder") or "").strip()
    if defaults_alias:
        return lookup_output_folder_alias(defaults_alias, config), defaults_alias

    defaults_raw = str(defaults.get("folder_token") or "").strip()
    return defaults_raw, ""


def _template_default_folder_fields(tpl: Dict[str, Any]) -> Tuple[str, str]:
    """Return (template_folder_token, template_output_folder) from a template dict."""
    return (
        str(tpl.get("folder_token") or "").strip(),
        str(tpl.get("output_folder") or "").strip(),
    )


def list_templates(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    config = config or load_config()
    templates = config.get("templates") or {}
    result = []
    for key, tpl in templates.items():
        if not isinstance(tpl, dict):
            continue
        tpl_raw, tpl_alias = _template_default_folder_fields(tpl)
        try:
            resolved_folder, resolved_alias = resolve_folder_token(
                template_folder_token=tpl_raw,
                template_output_folder=tpl_alias,
                config=config,
            )
        except TemplateResolveError:
            resolved_folder, resolved_alias = tpl_raw, tpl_alias
        result.append(
            {
                "key": key,
                "template_token": tpl.get("template_token") or "",
                "output_folder": resolved_alias or tpl_alias,
                "folder_token": resolved_folder,
                "document_name_pattern": tpl.get("document_name_pattern") or "",
                "party_b_full_name": tpl.get("party_b_full_name") or "",
                "template_type": str(tpl.get("type") or "docx"),
                "placeholder_count": len(tpl.get("placeholders") or {}),
            }
        )
    return result


def resolve_template(
    *,
    signing_unit: Optional[str] = None,
    template_token: Optional[str] = None,
    folder_token: Optional[str] = None,
    output_folder: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> ResolvedTemplate:
    """Resolve template from explicit tokens or signing_unit mapping."""
    config = config or load_config()
    defaults = config.get("defaults") or {}
    rate_limit = parse_rate_limit(defaults.get("rate_limit"))
    required = list(defaults.get("required_placeholders") or [])

    explicit_token = (template_token or "").strip()
    if explicit_token:
        folder, folder_alias = resolve_folder_token(
            request_folder_token=folder_token,
            request_output_folder=output_folder,
            config=config,
        )
        if not folder:
            raise TemplateResolveError(
                "folder_token or output_folder is required when using explicit template_token",
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
            output_folder=folder_alias,
            explicit=True,
        )

    unit = (signing_unit or "").strip()
    if not unit:
        # Fall back to defaults template_token for POC
        default_token = str(defaults.get("template_token") or "").strip()
        folder, folder_alias = resolve_folder_token(
            request_folder_token=folder_token,
            request_output_folder=output_folder,
            config=config,
        )
        if default_token and folder:
            return ResolvedTemplate(
                key="__defaults__",
                template_token=default_token,
                folder_token=folder,
                document_name_pattern=str(defaults.get("document_name_pattern") or "合同-{订单号}"),
                placeholders={},
                required_placeholders=required,
                rate_limit=rate_limit,
                output_folder=folder_alias,
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
    tpl_raw, tpl_alias = _template_default_folder_fields(tpl)
    folder, folder_alias = resolve_folder_token(
        request_folder_token=folder_token,
        request_output_folder=output_folder,
        template_folder_token=tpl_raw,
        template_output_folder=tpl_alias,
        config=config,
    )
    if not token:
        raise TemplateResolveError(
            f"template_token missing for template key={template_key!r}",
            error_code="MISSING_TEMPLATE_TOKEN",
        )
    if not folder:
        raise TemplateResolveError(
            f"folder_token / output_folder missing for template key={template_key!r}",
            error_code="MISSING_FOLDER_TOKEN",
        )

    tpl_required = tpl.get("required_placeholders")
    if isinstance(tpl_required, list):
        # Explicit list (including empty) overrides defaults
        required = [str(x) for x in tpl_required]

    sheet_cfg = tpl.get("sheet")
    if sheet_cfg is not None and not isinstance(sheet_cfg, dict):
        raise TemplateResolveError(
            f"sheet config must be a mapping for template key={template_key!r}",
            error_code="INVALID_SHEET_CONFIG",
        )

    template_type = str(tpl.get("type") or "docx").strip().lower() or "docx"
    if template_type in {"sheet", "sheets", "spreadsheet"}:
        template_type = "spreadsheet"
    elif template_type not in {"docx", "spreadsheet"}:
        raise TemplateResolveError(
            f"unsupported template type={template_type!r} for key={template_key!r}",
            error_code="INVALID_TEMPLATE_TYPE",
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
        template_type=template_type,
        append_detail_sheets=bool(tpl.get("append_detail_sheets")),
        detail_sheets=(
            dict(tpl["detail_sheets"])
            if isinstance(tpl.get("detail_sheets"), dict)
            else None
        ),
        output_folder=folder_alias,
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
        if field_name in {"执行人数", "活动人数", "报价人数"}:
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
