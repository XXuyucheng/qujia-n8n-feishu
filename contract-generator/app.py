"""Contract Generator — Feishu docx template copy + block placeholder fill."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from block_filler import build_update_requests, find_unresolved, probe_placeholders
from config_loader import (
    ConfigError,
    TemplateResolveError,
    build_placeholder_values,
    format_document_name,
    list_templates,
    load_config,
    missing_required,
    parse_rate_limit,
    resolve_template,
)
from feishu_client import FeishuAPIError, FeishuClient
from models import (
    GenerateRequest,
    GenerateResponse,
    PlaceholderHit,
    PreviewResponse,
    ProbeRequest,
    ProbeResponse,
)

CONFIG_PATH = Path(os.getenv("CONTRACT_GENERATOR_CONFIG_PATH", "/app/config.yaml"))
HOST = os.getenv("CONTRACT_GENERATOR_HOST", "0.0.0.0")
PORT = int(os.getenv("CONTRACT_GENERATOR_PORT", "8030"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("contract-generator")

app = FastAPI(title="Contract Generator", version="0.1.0")


def _get_config() -> Dict[str, Any]:
    try:
        return load_config(CONFIG_PATH)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _error_response(
    *,
    error_code: str,
    message: str,
    status_code: int = 400,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    body = GenerateResponse(
        success=False,
        error_code=error_code,
        message=message,
        details=details,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _resolve_from_request(req: GenerateRequest, config: Dict[str, Any]):
    return resolve_template(
        signing_unit=req.signing_unit,
        template_token=req.template_token,
        folder_token=req.folder_token,
        config=config,
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/templates")
def api_templates() -> Dict[str, Any]:
    config = _get_config()
    return {"templates": list_templates(config)}


@app.post("/api/probe", response_model=ProbeResponse)
def api_probe(req: ProbeRequest) -> Any:
    config = _get_config()
    defaults = config.get("defaults") or {}
    rate_limit = parse_rate_limit(defaults.get("rate_limit"))
    client = FeishuClient(rate_limit=rate_limit)
    try:
        token = client.resolve_token(req.tenant_access_token)
        # Probe reads the template itself (no copy)
        blocks = client.list_all_blocks(token, req.template_token)
        hits, warnings = probe_placeholders(blocks)
        text_count = sum(1 for b in blocks if isinstance(b, dict) and b.get("text"))
        return ProbeResponse(
            template_token=req.template_token,
            placeholders_found=[PlaceholderHit(**h) for h in hits],
            block_count=len(blocks),
            text_block_count=text_count,
            warnings=warnings,
        )
    except FeishuAPIError as exc:
        status = 403 if exc.error_code == "FEISHU_FORBIDDEN" else 502
        if exc.error_code == "MISSING_CREDENTIALS":
            status = 400
        return JSONResponse(
            status_code=status,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": exc.message,
                "details": exc.details,
            },
        )
    finally:
        client.close()


def _preview_payload(req: GenerateRequest) -> PreviewResponse:
    config = _get_config()
    try:
        tpl = _resolve_from_request(req, config)
    except TemplateResolveError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc

    values = build_placeholder_values(tpl, req.fields, req.placeholders)
    doc_name = format_document_name(
        tpl.document_name_pattern,
        fields=req.fields,
        document_name=req.document_name,
    )
    missing = missing_required(values, tpl.required_placeholders)
    warnings: List[str] = []
    if missing:
        warnings.append(f"缺少必填占位符: {', '.join(missing)}")

    return PreviewResponse(
        success=True,
        dry_run=True,
        document_name=doc_name,
        template_token=tpl.template_token,
        folder_token=tpl.folder_token,
        placeholders=values,
        missing_required=missing,
        warnings=warnings,
    )


@app.post("/api/generate/preview", response_model=PreviewResponse)
def api_generate_preview(req: GenerateRequest) -> PreviewResponse:
    return _preview_payload(req)


@app.post("/api/generate")
def api_generate(req: GenerateRequest) -> Any:
    if req.options and req.options.dry_run:
        preview = _preview_payload(req)
        return GenerateResponse(
            success=True,
            document_name=preview.document_name,
            template_token=preview.template_token,
            placeholders=preview.placeholders,
            unresolved_placeholders=preview.missing_required,
            warnings=preview.warnings,
        )

    config = _get_config()
    try:
        tpl = _resolve_from_request(req, config)
    except TemplateResolveError as exc:
        return _error_response(error_code=exc.error_code, message=exc.message)

    values = build_placeholder_values(tpl, req.fields, req.placeholders)
    still_missing = missing_required(values, tpl.required_placeholders)
    if still_missing:
        return _error_response(
            error_code="MISSING_REQUIRED_PLACEHOLDER",
            message=f"必填占位符未解析: {', '.join(still_missing)}",
            details={"missing": still_missing, "signing_unit": req.signing_unit},
        )

    doc_name = format_document_name(
        tpl.document_name_pattern,
        fields=req.fields,
        document_name=req.document_name,
    )

    client = FeishuClient(rate_limit=tpl.rate_limit)
    try:
        token = client.resolve_token(req.tenant_access_token)
        copied = client.copy_file(
            token,
            template_token=tpl.template_token,
            folder_token=tpl.folder_token,
            name=doc_name,
        )
        document_id = str(copied["token"])
        document_url = str(
            copied.get("url") or f"https://feishu.cn/docx/{document_id}"
        )

        blocks = client.list_all_blocks(token, document_id)
        update_requests = build_update_requests(blocks, values)
        if update_requests:
            client.batch_update(token, document_id, update_requests)

        # Re-check unresolved against original blocks with values applied conceptually:
        # keys still in original text that were not provided.
        unresolved = find_unresolved(blocks, values)
        warnings: List[str] = []
        if unresolved and not (req.options and req.options.skip_unresolved_warnings):
            warnings.append(f"文档中仍有未提供值的占位符: {', '.join(unresolved)}")

        return GenerateResponse(
            success=True,
            document_id=document_id,
            file_token=document_id,
            document_url=document_url,
            document_name=doc_name,
            template_token=tpl.template_token,
            replaced_blocks=len(update_requests),
            unresolved_placeholders=unresolved,
            warnings=warnings,
            placeholders=values,
        )
    except FeishuAPIError as exc:
        status = 502
        if exc.error_code in {"MISSING_CREDENTIALS", "MISSING_TOKEN"}:
            status = 400
        elif exc.error_code == "FEISHU_FORBIDDEN":
            status = 403
        elif exc.error_code == "FEISHU_RATE_LIMITED":
            status = 429
        return _error_response(
            error_code=exc.error_code,
            message=exc.message,
            status_code=status,
            details=exc.details,
        )
    finally:
        client.close()


if __name__ == "__main__":
    import uvicorn

    # Local default: use repo config.yaml when not in Docker
    if not CONFIG_PATH.exists():
        local = Path(__file__).resolve().parent / "config.yaml"
        if local.exists():
            os.environ["CONTRACT_GENERATOR_CONFIG_PATH"] = str(local)
            globals()["CONFIG_PATH"] = local

    uvicorn.run(app, host=HOST, port=PORT)
