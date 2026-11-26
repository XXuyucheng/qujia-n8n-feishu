"""Decide placeholder values before they are written into a docx."""

from __future__ import annotations


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

    `required` is ignored. Blank values that were sent become empty_optional.
    Placeholders absent from the request are filled later as `/`.
    """
    del required
    values = {str(key): _text(raw) for key, raw in placeholders.items()}
    filled: dict[str, str] = {}
    for key, raw in values.items():
        filled[key] = empty_optional if not raw.strip() else raw
    return filled
