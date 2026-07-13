#!/usr/bin/env python3
"""Merge Feishu field meta JSON into config.yaml."""

import json
import sys
from pathlib import Path

import yaml

KEEP_PROPERTY_KEYS = {
    "options",
    "dateFormat",
    "displayTimeZone",
    "autoFill",
    "dataType",
    "formatter",
    "refTableId",
    "refFieldId",
    "tableId",
    "multiple",
    "backFieldId",
}


def sanitize_field(field: dict) -> dict:
    item = {
        "id": field["id"],
        "type": field["type"],
        "name": field["name"],
    }
    prop = field.get("property")
    if isinstance(prop, dict) and prop:
        cleaned = {k: prop[k] for k in KEEP_PROPERTY_KEYS if k in prop}
        if cleaned:
            item["property"] = cleaned
    return item


def main() -> None:
    if len(sys.argv) != 5:
        print(
            "Usage: merge_table_fields.py <fields.json> <table_key> <table_name> <table_id>",
            file=sys.stderr,
        )
        sys.exit(1)

    fields_path = Path(sys.argv[1])
    table_key = sys.argv[2]
    table_name = sys.argv[3]
    table_id = sys.argv[4]
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"

    fields = json.loads(fields_path.read_text(encoding="utf-8"))
    if not isinstance(fields, list):
        raise SystemExit("fields.json must be an array")

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    config.setdefault("tables", {})[table_key] = {
        "name": table_name,
        "table_id": table_id,
        "fields": [sanitize_field(field) for field in fields if field.get("id")],
    }

    config_path.write_text(
        yaml.dump(config, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    print(f"Updated {config_path} with {len(fields)} fields for {table_key}")


if __name__ == "__main__":
    main()
