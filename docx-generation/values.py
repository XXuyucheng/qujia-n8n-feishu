"""Decide placeholder values before they are written into a docx."""

from __future__ import annotations


class MissingRequired(Exception):
    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(", ".join(self.missing))


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def prepare_values(
    placeholders: dict[str, str],
    required: list[str] | None = None,
    empty_optional: str = "",
) -> dict[str, str]:
    """Return substitution values.

    Blank required keys raise MissingRequired and do not become empty_optional.
    Blank keys that are present and not required become empty_optional.
    """
    required_keys = list(required or [])
    values = {str(key): _text(raw) for key, raw in placeholders.items()}
    required_set = set(required_keys)
    missing: list[str] = []
    seen: set[str] = set()
    for key in required_keys:
        if key in seen:
            continue
        seen.add(key)
        if not values.get(key, "").strip():
            missing.append(key)
    if missing:
        raise MissingRequired(missing)

    filled: dict[str, str] = {}
    for key, raw in values.items():
        if key not in required_set and not raw.strip():
            filled[key] = empty_optional
        else:
            filled[key] = raw
    return filled
