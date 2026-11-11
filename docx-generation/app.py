"""docx-generation — fill {{key}} placeholders in a local .docx template."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from fill import InvalidDocx, fill_docx
from values import MissingRequired, prepare_values

HOST = os.getenv("DOCX_GENERATION_HOST", "0.0.0.0")
PORT = int(os.getenv("DOCX_GENERATION_PORT", "8060"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("docx-generation")

app = FastAPI(title="docx-generation", version="0.1.0")


class GenerateBody(BaseModel):
    template: str
    placeholders: dict[str, str]
    required: list[str] = []
    empty_optional: str = ""


class TemplatePathInvalid(Exception):
    def __init__(self, template: str):
        self.template = template
        super().__init__(template)


class TemplateMissing(Exception):
    def __init__(self, template: str):
        self.template = template
        super().__init__(template)


def templates_dir() -> Path:
    return Path(os.getenv("DOCX_TEMPLATES_DIR", "/app/templates"))


def resolve_template(template: str) -> Path:
    raw = template.strip()
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or any(part == ".." for part in path.parts)
        or path.suffix.lower() != ".docx"
    ):
        raise TemplatePathInvalid(raw)
    root = templates_dir().resolve()
    try:
        candidate = (root / path).resolve()
    except OSError as exc:
        raise TemplatePathInvalid(raw) from exc
    if not candidate.is_relative_to(root):
        raise TemplatePathInvalid(raw)
    if not candidate.is_file():
        raise TemplateMissing(raw)
    return candidate


def content_disposition(name: str) -> str:
    base = Path(name).name.replace("\r", "").replace("\n", "").replace('"', "")
    if not base or base in {".", ".."}:
        base = "filled.docx"
    encoded = quote(base, safe="")
    ascii_name = base.encode("ascii", "ignore").decode()
    if not ascii_name.lower().endswith(".docx"):
        ascii_name = "filled.docx"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _error(status_code: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


def _docx_response(data: bytes, filename: str, unresolved: list[str]) -> Response:
    headers = {"Content-Disposition": content_disposition(filename)}
    if unresolved:
        headers["X-Docx-Unresolved"] = quote(
            json.dumps(unresolved, ensure_ascii=False),
            safe="",
        )
    return Response(content=data, media_type=DOCX_MEDIA_TYPE, headers=headers)


def _fill_response(
    data: bytes,
    placeholders: dict[str, str],
    required: list[str],
    empty_optional: str,
    filename: str,
) -> Response:
    try:
        values = prepare_values(placeholders, required, empty_optional)
    except MissingRequired as exc:
        return _error(400, {"error": "missing_required", "missing": exc.missing})
    try:
        filled, unresolved = fill_docx(data, values)
    except InvalidDocx:
        return _error(400, {"error": "invalid_template"})
    if unresolved:
        logger.info("unresolved placeholders: %s", ",".join(unresolved))
    return _docx_response(filled, filename, unresolved)


def _invalid_placeholders(message: str = "invalid") -> JSONResponse:
    return _error(400, {"error": "invalid_placeholders", "message": message})


async def _from_json(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _invalid_placeholders("request body is not json")
    if not isinstance(payload, dict):
        return _invalid_placeholders("request body must be an object")
    try:
        body = GenerateBody.model_validate(payload)
    except ValidationError as exc:
        message = exc.errors()[0]["msg"] if exc.errors() else "invalid"
        return _invalid_placeholders(message)
    try:
        path = resolve_template(body.template)
    except TemplatePathInvalid as exc:
        return _error(400, {"error": "template_path_invalid", "template": exc.template})
    except TemplateMissing as exc:
        return _error(400, {"error": "template_not_found", "template": exc.template})
    try:
        data = path.read_bytes()
    except OSError:
        return _error(400, {"error": "template_not_found", "template": body.template})
    return _fill_response(
        data,
        body.placeholders,
        body.required,
        body.empty_optional,
        path.name,
    )


async def _from_multipart(request: Request) -> Response:
    try:
        form = await request.form()
    except Exception:
        return _invalid_placeholders("multipart body is invalid")
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _error(400, {"error": "invalid_template"})
    if "placeholders" not in form:
        return _invalid_placeholders("placeholders is required")
    try:
        placeholders = json.loads(str(form.get("placeholders") or ""))
        required_raw = form.get("required")
        required = [] if required_raw in (None, "") else json.loads(str(required_raw))
        empty_raw = form.get("empty_optional")
        empty_optional = "" if empty_raw is None else str(empty_raw)
        body = GenerateBody.model_validate(
            {
                "template": "upload.docx",
                "placeholders": placeholders,
                "required": required,
                "empty_optional": empty_optional,
            }
        )
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        message = "invalid"
        if isinstance(exc, ValidationError) and exc.errors():
            message = exc.errors()[0]["msg"]
        return _invalid_placeholders(message)
    data = await upload.read()
    await upload.close()
    filename = getattr(upload, "filename", None) or "filled.docx"
    return _fill_response(
        data,
        body.placeholders,
        body.required,
        body.empty_optional,
        filename,
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/generate")
async def api_generate(request: Request) -> Response:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await _from_json(request)
    if "multipart/form-data" in content_type:
        return await _from_multipart(request)
    return _error(400, {"error": "unsupported_media_type"})


if __name__ == "__main__":
    import uvicorn

    logger.info("templates dir %s", templates_dir())
    uvicorn.run(app, host=HOST, port=PORT)
