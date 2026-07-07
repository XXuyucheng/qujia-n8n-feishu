import ast
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse


CONFIG_PATH = Path(os.getenv("FIELD_PARSER_CONFIG_PATH", "/app/config.yaml"))
HOST = os.getenv("FIELD_PARSER_HOST", "0.0.0.0")
PORT = int(os.getenv("FIELD_PARSER_PORT", "8020"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-field-parser")

app = FastAPI(title="Feishu Field Parser")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=500, detail=f"Config not found: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    tables = data.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise HTTPException(status_code=500, detail="Config must contain non-empty tables mapping")

    return data


def get_table_config(table_key: str) -> Dict[str, Any]:
    config = load_config()
    table = (config.get("tables") or {}).get(table_key)
    if not isinstance(table, dict):
        raise HTTPException(status_code=404, detail=f"Unknown table_key: {table_key}")
    fields = table.get("fields")
    if not isinstance(fields, list):
        raise HTTPException(status_code=500, detail=f"Table {table_key} must contain fields list")
    return table


def table_index(table: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(field.get("id")): field
        for field in table.get("fields", [])
        if isinstance(field, dict) and field.get("id")
    }


def parse_input_records(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        records = value
    elif isinstance(value, str):
        records = parse_js_like_value(value)
    else:
        raise HTTPException(status_code=400, detail="records must be an array or JSON/JS string")

    if not isinstance(records, list):
        raise HTTPException(status_code=400, detail="records must parse to an array")

    normalized = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise HTTPException(status_code=400, detail=f"records[{index}] is not an object")
        normalized.append(record)
    return normalized


def parse_js_like_value(value: str) -> Any:
    text = value.strip()
    if not text:
        return []

    assignment = re.match(r"^(?:const|let|var)?\s*[\w$]+\s*=\s*(.*?);?\s*$", text, re.S)
    if assignment:
        text = assignment.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise HTTPException(status_code=400, detail=f"Input is not valid JSON/JS data: {exc}") from exc


def parse_field_value(record: Dict[str, Any], meta: Dict[str, Any], warnings: List[str]) -> Any:
    raw = record.get("field_value")
    parsed = parse_raw_value(raw, warnings)

    identity_users = ((record.get("field_identity_value") or {}).get("users") or [])
    if identity_users and is_user_field(meta):
        return normalize_users(identity_users)

    field_type = int_or_none(meta.get("type"))
    bus_type, data_values = extract_bus_data(parsed)
    value = collapse_values(data_values) if data_values is not None else parsed

    if field_type in {3, 4} or bus_type == 3 or formula_data_type(meta) == 3:
        return map_option_value(value, meta)

    if is_user_field(meta) or bus_type == 11:
        return normalize_users(value)

    if is_date_field(meta) or bus_type == 5:
        return normalize_date_value(value)

    if field_type == 1005:
        return normalize_auto_number(value)

    if field_type in {2, 19, 20} or bus_type == 202:
        return normalize_number_like(value, meta, bus_type)

    return normalize_generic_value(value, meta)


def parse_raw_value(raw: Any, warnings: List[str]) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw

    text = raw.strip()
    if text == "":
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw
    except Exception as exc:
        warnings.append(f"Failed to parse field_value as JSON: {exc}")
        return raw


def extract_bus_data(value: Any) -> Tuple[Optional[int], Optional[List[Any]]]:
    if not isinstance(value, dict):
        return None, None

    bus_types = value.get("bus_type")
    data = value.get("data")
    if not isinstance(data, list):
        return None, None

    bus_type = None
    if isinstance(bus_types, list) and bus_types:
        bus_type = int_or_none(bus_types[0])
    elif bus_types is not None:
        bus_type = int_or_none(bus_types)

    return bus_type, data


def collapse_values(values: Any) -> Any:
    if not isinstance(values, list):
        return values

    cleaned = [normalize_nested_value(item) for item in values]
    if len(cleaned) == 0:
        return None
    if len(cleaned) == 1:
        return cleaned[0]
    return cleaned


def normalize_nested_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value.keys()) == {"value"}:
        return value.get("value")
    return value


def map_option_value(value: Any, meta: Dict[str, Any]) -> Any:
    option_map = {
        str(option.get("id")): option.get("name")
        for option in ((meta.get("property") or {}).get("options") or [])
        if isinstance(option, dict) and option.get("id")
    }

    def convert(one: Any) -> Any:
        if one is None:
            return None
        if isinstance(one, dict):
            option_id = one.get("id") or one.get("option_id") or one.get("value")
            if option_id is not None and str(option_id) in option_map:
                return option_map[str(option_id)]
            return normalize_generic_value(one, meta)
        return option_map.get(str(one), one)

    if isinstance(value, list):
        mapped = [convert(item) for item in value]
        return mapped[0] if len(mapped) == 1 else mapped
    return convert(value)


def normalize_users(value: Any) -> Any:
    if value is None:
        return None

    users = value
    if isinstance(value, dict):
        users = value.get("users") or value.get("data") or value
    if not isinstance(users, list):
        users = [users]

    normalized = []
    for user in users:
        if not isinstance(user, dict):
            normalized.append(user)
            continue

        user_id = user.get("user_id") or user.get("userId")
        if isinstance(user_id, dict):
            ids = user_id
        else:
            ids = {"user_id": user_id} if user_id else {}

        normalized.append(
            {
                "name": user.get("name"),
                "en_name": user.get("en_name") or user.get("enName"),
                "user_id": ids.get("user_id"),
                "open_id": ids.get("open_id"),
                "union_id": ids.get("union_id"),
                "avatar_url": user.get("avatar_url") or user.get("avatarUrl"),
            }
        )

    return normalized[0] if len(normalized) == 1 else normalized


def normalize_date_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        converted = [normalize_date_value(item) for item in value]
        return converted[0] if len(converted) == 1 else converted
    if isinstance(value, dict):
        if "value" in value:
            return normalize_date_value(value.get("value"))
        return normalize_generic_value(value, {})

    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip()):
        timestamp = float(value.strip())
    else:
        return value

    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000

    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_auto_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        converted = [normalize_auto_number(item) for item in value]
        return converted[0] if len(converted) == 1 else converted
    if isinstance(value, dict):
        return value.get("number") or value.get("sequence") or normalize_generic_value(value, {})
    return value


def normalize_number_like(value: Any, meta: Dict[str, Any], bus_type: Optional[int]) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        converted = [normalize_number_like(item, meta, bus_type) for item in value]
        return converted[0] if len(converted) == 1 else converted

    data_type = (((meta.get("property") or {}).get("dataType") or {}).get("type"))
    if int_or_none(data_type) == 201 or bus_type == 201:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
        number = float(value)
        return int(number) if number.is_integer() else number
    return normalize_generic_value(value, meta)


def normalize_generic_value(value: Any, meta: Dict[str, Any]) -> Any:
    if isinstance(value, list):
        converted = [normalize_generic_value(item, meta) for item in value]
        return converted[0] if len(converted) == 1 else converted
    if isinstance(value, dict):
        if set(value.keys()) == {"value"}:
            return normalize_generic_value(value.get("value"), meta)
        return value
    return value


def is_date_field(meta: Dict[str, Any]) -> bool:
    return int_or_none(meta.get("type")) in {5, 1001, 1002}


def is_user_field(meta: Dict[str, Any]) -> bool:
    return int_or_none(meta.get("type")) in {11, 1003}


def formula_data_type(meta: Dict[str, Any]) -> Optional[int]:
    data_type = (((meta.get("property") or {}).get("dataType") or {}).get("type"))
    return int_or_none(data_type)


def int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_records(table_key: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    table = get_table_config(table_key)
    fields_by_id = table_index(table)
    output: Dict[str, Any] = {}
    unknown_fields: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for record in records:
        field_id = record.get("field_id")
        if not field_id:
            warnings.append("Skipped record without field_id")
            continue

        meta = fields_by_id.get(str(field_id))
        if not meta:
            unknown_fields.append(record)
            continue

        name = str(meta.get("name") or field_id)
        if name in output:
            warnings.append(f"Duplicate field name {name}; later value overwrote earlier value")
        output[name] = parse_field_value(record, meta, warnings)

    result: Dict[str, Any] = {
        "table_key": table_key,
        "table_name": table.get("name") or table_key,
        "object": output,
        "warnings": warnings,
    }
    if unknown_fields:
        result["_unknown_fields"] = unknown_fields
    return result


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/tables")
def api_tables() -> Dict[str, Any]:
    config = load_config()
    tables = []
    for key, table in (config.get("tables") or {}).items():
        fields = table.get("fields") or []
        tables.append(
            {
                "key": key,
                "name": table.get("name") or key,
                "field_count": len(fields),
            }
        )
    return {"tables": tables}


@app.post("/api/parse")
def api_parse(payload: Dict[str, Any]) -> Dict[str, Any]:
    table_key = str(payload.get("table_key") or "").strip()
    if not table_key:
        raise HTTPException(status_code=400, detail="table_key is required")

    records = parse_input_records(payload.get("records"))
    return parse_records(table_key, records)


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    sample = json.dumps(SAMPLE_INPUT, ensure_ascii=False, indent=2)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Feishu Field Parser</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #1f2328; }}
    header {{ padding: 20px 28px; background: #111827; color: white; }}
    main {{ padding: 24px 28px; max-width: 1280px; margin: 0 auto; }}
    .bar {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }}
    select, button {{ font: inherit; padding: 8px 10px; border-radius: 8px; border: 1px solid #d0d7de; }}
    button {{ border: 0; background: #2563eb; color: white; cursor: pointer; }}
    button.secondary {{ background: #4b5563; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    textarea, pre {{ width: 100%; min-height: 620px; box-sizing: border-box; border: 1px solid #d0d7de; border-radius: 10px; background: white; padding: 14px; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    textarea {{ resize: vertical; }}
    pre {{ overflow: auto; white-space: pre-wrap; }}
    .hint {{ color: #6b7280; margin: 8px 0 18px; }}
    .error {{ color: #b42318; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Feishu Field Parser</h1>
    <div>把飞书 field_id/field_value 数组解析为字段名对象</div>
  </header>
  <main>
    <div class="bar">
      <label>表格 <select id="table"></select></label>
      <button onclick="parseNow()">解析</button>
      <button class="secondary" onclick="loadSample()">载入示例</button>
      <span id="status" class="hint"></span>
    </div>
    <p class="hint">左侧粘贴飞书给出的 JS/JSON 数组，右侧会输出可直接复制到 n8n Code 节点使用的对象。</p>
    <div class="grid">
      <textarea id="input" spellcheck="false"></textarea>
      <pre id="output">等待解析...</pre>
    </div>
  </main>
  <script>
    const sampleInput = {json.dumps(sample, ensure_ascii=False)};

    async function loadTables() {{
      const res = await fetch('/api/tables');
      const data = await res.json();
      const select = document.getElementById('table');
      select.innerHTML = '';
      for (const table of data.tables || []) {{
        const opt = document.createElement('option');
        opt.value = table.key;
        opt.textContent = `${{table.name}} (${{table.field_count}} fields)`;
        select.appendChild(opt);
      }}
    }}

    function loadSample() {{
      document.getElementById('input').value = sampleInput;
    }}

    async function parseNow() {{
      const status = document.getElementById('status');
      const output = document.getElementById('output');
      status.textContent = '解析中...';
      output.className = '';
      try {{
        const res = await fetch('/api/parse', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            table_key: document.getElementById('table').value,
            records: document.getElementById('input').value,
          }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);
        output.textContent = JSON.stringify(data.object, null, 2);
        status.textContent = data.warnings?.length ? `完成，${{data.warnings.length}} 条提示` : '完成';
      }} catch (err) {{
        output.textContent = String(err);
        output.className = 'error';
        status.textContent = '失败';
      }}
    }}

    loadTables().then(loadSample);
  </script>
</body>
</html>"""
    )


SAMPLE_INPUT = [
    {"field_id": "fldw9sa0WQ", "field_value": "optgUHwD7r"},
    {"field_id": "fldAbJyX5V", "field_value": '{"bus_type":[202],"data":[0.0]}'},
    {
        "field_id": "fldSl0FQg7",
        "field_identity_value": {
            "users": [
                {
                    "avatar_url": "https://example.com/avatar.jpg",
                    "en_name": "魏航杰",
                    "name": "魏航杰",
                    "user_id": {
                        "open_id": "ou_f6e6ae41f917b4e282ef4951cfe2cf6f",
                        "union_id": "on_2d7e2db5752950a26bdeb8ae600729ae",
                        "user_id": "a6fcb8bb",
                    },
                }
            ]
        },
        "field_value": '{"users":[{"userId":"7524554713046745089","name":"魏航杰","enName":"魏航杰"}]}',
    },
    {"field_id": "fldpeuv5SE", "field_value": "1782268447000"},
    {"field_id": "fldF1byWPT", "field_value": '[{"sequence":"1538","number":"SR26061538"}]'},
]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
