"""Traverse Feishu docx blocks and replace {{placeholders}}."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
OPEN_BRACE_RE = re.compile(r"\{\{")
CLOSE_BRACE_RE = re.compile(r"\}\}")


def replace_in_block(block: Dict[str, Any], values: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """If block text changes, return a batch_update request; else None."""
    text_obj = block.get("text")
    if not isinstance(text_obj, dict) or not text_obj.get("elements"):
        return None

    changed = False
    new_elements: List[Dict[str, Any]] = []

    for element in text_obj["elements"]:
        if not isinstance(element, dict):
            new_elements.append(element)
            continue

        text_run = element.get("text_run")
        if not isinstance(text_run, dict):
            # mention_doc, equation, etc. — keep as-is
            new_elements.append(element)
            continue

        content = text_run.get("content", "")
        if not isinstance(content, str):
            content = str(content) if content is not None else ""

        def _sub(match: re.Match) -> str:
            key = match.group(1).strip()
            if key in values:
                return str(values[key])
            return match.group(0)

        new_content = PLACEHOLDER_RE.sub(_sub, content)
        if new_content != content:
            changed = True

        style = text_run.get("text_element_style")
        if style is None:
            style = {}

        new_elements.append(
            {
                "text_run": {
                    "content": new_content,
                    "text_element_style": style,
                }
            }
        )

    if not changed:
        return None

    return {
        "block_id": block["block_id"],
        "update_text_elements": {"elements": new_elements},
    }


def build_update_requests(
    blocks: List[Dict[str, Any]],
    values: Dict[str, str],
) -> List[Dict[str, Any]]:
    requests: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        req = replace_in_block(block, values)
        if req:
            requests.append(req)
    return requests


def _block_text_runs(block: Dict[str, Any]) -> List[str]:
    text_obj = block.get("text")
    if not isinstance(text_obj, dict):
        return []
    runs = []
    for element in text_obj.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run")
        if isinstance(text_run, dict) and isinstance(text_run.get("content"), str):
            runs.append(text_run["content"])
    return runs


def probe_placeholders(
    blocks: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Scan blocks for {{key}} placeholders and split-brace warnings."""
    hits: List[Dict[str, str]] = []
    warnings: List[str] = []
    seen: set = set()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_id = str(block.get("block_id") or "")
        runs = _block_text_runs(block)
        if not runs:
            continue

        # Detect split braces across text_runs in the same block
        open_runs = [i for i, r in enumerate(runs) if "{{" in r and "}}" not in r]
        close_runs = [i for i, r in enumerate(runs) if "}}" in r and "{{" not in r]
        if open_runs or close_runs:
            # Also check if any run has incomplete pair
            for i, r in enumerate(runs):
                opens = len(OPEN_BRACE_RE.findall(r))
                closes = len(CLOSE_BRACE_RE.findall(r))
                if opens != closes:
                    warnings.append(
                        f"block {block_id}: 占位符可能跨 text_run（run[{i}] 花括号不成对）"
                    )
                    break

        for content in runs:
            for match in PLACEHOLDER_RE.finditer(content):
                key = match.group(1).strip()
                sample = content
                if len(sample) > 80:
                    sample = sample[:77] + "..."
                dedupe = (block_id, key, sample)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                hits.append({"key": key, "block_id": block_id, "sample": sample})

    return hits, warnings


def find_unresolved(
    blocks: List[Dict[str, Any]],
    values: Dict[str, str],
) -> List[str]:
    """Return placeholder keys present in blocks but missing from values."""
    unresolved: List[str] = []
    seen: set = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for content in _block_text_runs(block):
            for match in PLACEHOLDER_RE.finditer(content):
                key = match.group(1).strip()
                if key in values and str(values[key]).strip() != "":
                    continue
                if key not in seen:
                    seen.add(key)
                    unresolved.append(key)
    return unresolved


def apply_values_to_content(content: str, values: Dict[str, str]) -> str:
    def _sub(match: re.Match) -> str:
        key = match.group(1).strip()
        if key in values:
            return str(values[key])
        return match.group(0)

    return PLACEHOLDER_RE.sub(_sub, content)
