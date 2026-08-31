"""Feishu Files Generation — Feishu docx template copy + block placeholder fill."""

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
    list_output_folders,
    list_templates,
    load_config,
    missing_required,
    parse_rate_limit,
    resolve_template,
)
from feishu_client import FeishuAPIError, FeishuClient
from contract_sheet_filler import (
    append_contract_detail_sheets,
    resolve_settlement_rows,
)
from models import (
    GenerateRequest,
    GenerateResponse,
    PlaceholderHit,
    PreviewResponse,
    ProbeRequest,
    ProbeResponse,
)
from sheet_filler import (
    SheetConfigError,
    build_formula_ranges,
    build_sheet_value_ranges,
    find_sheet_refs,
    resolve_sheet_rows,
    unused_row_delete_range,
)
from spreadsheet_filler import (
    anchor_rows_to_delete,
    build_activity_total_formula_ranges,
    build_detail_formula_ranges,
    build_final_total_formula_ranges,
    build_itinerary_value_ranges,
    build_online_quote_detail_ranges,
    build_placeholder_value_ranges,
    detail_start_row_after_anchor_delete,
    find_total_label_row,
    insert_row_spec,
    itinerary_insert_before_row,
    normalize_itinerary_rows,
    normalize_quote_sheet_rows,
)

CONFIG_PATH = Path(os.getenv("FEISHU_FILES_GENERATION_CONFIG_PATH", "/app/config.yaml"))
HOST = os.getenv("FEISHU_FILES_GENERATION_HOST", "0.0.0.0")
PORT = int(os.getenv("FEISHU_FILES_GENERATION_PORT", "8030"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-files-generation")

app = FastAPI(title="Feishu Files Generation", version="0.1.0")


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
        output_folder=req.output_folder,
        config=config,
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/templates")
def api_templates() -> Dict[str, Any]:
    config = _get_config()
    return {"templates": list_templates(config)}


@app.get("/api/output-folders")
def api_output_folders() -> Dict[str, Any]:
    config = _get_config()
    return {"output_folders": list_output_folders(config)}


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
        sheet_refs = find_sheet_refs(blocks)
        return ProbeResponse(
            template_token=req.template_token,
            placeholders_found=[PlaceholderHit(**h) for h in hits],
            block_count=len(blocks),
            text_block_count=text_count,
            sheet_block_count=len(sheet_refs),
            sheet_tokens=[r["raw_token"] for r in sheet_refs],
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
        output_folder=tpl.output_folder or None,
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
            folder_token=preview.folder_token,
            output_folder=preview.output_folder,
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
        if tpl.template_type == "spreadsheet":
            return _generate_spreadsheet(client, token, tpl, req, values, doc_name)
        return _generate_docx(client, token, tpl, req, values, doc_name)
    except SheetConfigError as exc:
        return _error_response(
            error_code=exc.error_code,
            message=exc.message,
            status_code=400,
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


def _generate_spreadsheet(
    client: FeishuClient,
    token: str,
    tpl: Any,
    req: GenerateRequest,
    values: Dict[str, str],
    doc_name: str,
) -> Any:
    """Copy standalone spreadsheet template, replace {{}} headers, insert+write detail."""
    copied = client.copy_file(
        token,
        template_token=tpl.template_token,
        folder_token=tpl.folder_token,
        name=doc_name,
        file_type="sheet",
    )
    spreadsheet_token = str(copied["token"])
    document_url = str(
        copied.get("url") or f"https://feishu.cn/sheets/{spreadsheet_token}"
    )

    sheets = client.list_spreadsheet_sheets(token, spreadsheet_token)
    if not sheets:
        return _error_response(
            error_code="SHEET_NOT_FOUND",
            message="复制后的电子表格中未找到 worksheet",
            details={"template_key": tpl.key},
        )
    first = sheets[0]
    sheet_id = str(
        first.get("sheet_id")
        or (first.get("sheet") or {}).get("sheet_id")
        or first.get("sheetId")
        or ""
    )
    if not sheet_id:
        return _error_response(
            error_code="SHEET_NOT_FOUND",
            message="无法解析 worksheet sheet_id",
            details={"sheets": sheets, "template_key": tpl.key},
        )

    warnings: List[str] = []
    replaced_cells = 0

    # Scan a wide range for header placeholders before inserting rows
    scan_range = f"{sheet_id}!A1:Z40"
    grid = client.get_spreadsheet_values(token, spreadsheet_token, scan_range)
    ph_ranges, unresolved_ph = build_placeholder_value_ranges(sheet_id, grid, values)
    if ph_ranges:
        client.values_batch_update(token, spreadsheet_token, ph_ranges)
        replaced_cells = len(ph_ranges)
    if unresolved_ph and not (req.options and req.options.skip_unresolved_warnings):
        warnings.append(f"表格中仍有未提供值的占位符: {', '.join(unresolved_ph)}")

    sheet_rows_written = 0
    sheet_rows = normalize_quote_sheet_rows(
        sheet_rows=req.sheet_rows,
        fields=req.fields,
    )
    if sheet_rows and not tpl.sheet:
        warnings.append("请求含明细行，但模板未配置 sheet，已跳过明细写入")
    elif sheet_rows and tpl.sheet:
        insert_spec = insert_row_spec(tpl.sheet, len(sheet_rows))
        if insert_spec is not None:
            start_index, end_index = insert_spec
            client.insert_dimension_range(
                token,
                spreadsheet_token,
                sheet_id,
                start_index,
                end_index,
                inherit_style="BEFORE",
            )
        detail_ranges = build_online_quote_detail_ranges(
            sheet_id,
            tpl.sheet,
            sheet_rows,
        )
        if detail_ranges:
            client.values_batch_update(token, spreadsheet_token, detail_ranges)
            sheet_rows_written = len(sheet_rows)

        # Remove blank insert anchors (rows 9/10) so detail sits flush with neighbors
        for del_start, del_end in anchor_rows_to_delete(tpl.sheet, len(sheet_rows)) or []:
            client.delete_dimension_range(
                token,
                spreadsheet_token,
                sheet_id,
                del_start,
                del_end,
            )

        # Formulas use post-delete row numbers (detail starts at insert_after_row)
        formula_sheet = {
            **tpl.sheet,
            "start_row": detail_start_row_after_anchor_delete(tpl.sheet),
        }

        # inheritStyle only copies appearance — write line totals + total-row formulas
        formula_cfg = formula_sheet.get("formula") if isinstance(formula_sheet.get("formula"), dict) else {}
        total_label = str(
            (formula_cfg or {}).get("total_label") or "活动总价（含税）"
        ).strip()
        tax_label = str(
            (formula_cfg or {}).get("tax_label") or "税费及服务"
        ).strip()
        final_total_label = str(
            (formula_cfg or {}).get("final_total_label") or ""
        ).strip()
        total_row = None
        tax_row = None
        final_row = None
        if total_label or tax_label or final_total_label:
            post_grid = client.get_spreadsheet_values(
                token,
                spreadsheet_token,
                f"{sheet_id}!A1:Z120",
            )
            if total_label:
                total_row = find_total_label_row(post_grid, total_label, col_index=0)
            if tax_label:
                tax_row = find_total_label_row(post_grid, tax_label, col_index=0)
            if final_total_label:
                final_row = find_total_label_row(
                    post_grid, final_total_label, col_index=0
                )

        formula_ranges = build_detail_formula_ranges(
            sheet_id,
            formula_sheet,
            len(sheet_rows),
            exclude_rows=[tax_row] if tax_row is not None else None,
        )
        formula_ranges.extend(
            build_activity_total_formula_ranges(
                sheet_id,
                formula_sheet,
                len(sheet_rows),
                total_row=total_row,
                tax_row=tax_row,
            )
        )
        formula_ranges.extend(
            build_final_total_formula_ranges(
                sheet_id,
                formula_sheet,
                total_row=total_row,
                final_row=final_row,
            )
        )
        if formula_ranges:
            client.values_batch_update(token, spreadsheet_token, formula_ranges)
        if total_row is None:
            warnings.append(
                f"未在 A 列找到「{total_label}」，已跳过活动总价行公式写入"
            )
        elif tax_row is None:
            warnings.append(
                f"未在 A 列找到「{tax_label}」，活动总价 D 列仅写入 SUM(F)（未乘税费）"
            )
        if final_total_label and final_row is None:
            warnings.append(
                f"未在 A 列找到「{final_total_label}」，已跳过最终总价默认公式写入"
            )
        elif final_total_label and total_row is None:
            warnings.append(
                f"已找到「{final_total_label}」但缺少活动总价行，已跳过最终总价默认公式"
            )

    itinerary_rows_written = 0
    itinerary_rows = normalize_itinerary_rows(
        itinerary_rows=req.itinerary_rows,
        fields=req.fields,
    )
    if itinerary_rows and not (tpl.sheet and isinstance(tpl.sheet.get("itinerary"), dict)):
        warnings.append("请求含行程明细，但模板未配置 sheet.itinerary，已跳过行程写入")
    elif itinerary_rows and tpl.sheet:
        try:
            before = itinerary_insert_before_row(tpl.sheet, len(sheet_rows))
            itin_ranges = build_itinerary_value_ranges(
                sheet_id,
                tpl.sheet,
                itinerary_rows,
                start_row=before,
            )
        except SheetConfigError as exc:
            return _error_response(
                error_code=exc.error_code,
                message=exc.message,
                status_code=400,
            )
        client.insert_dimension_range(
            token,
            spreadsheet_token,
            sheet_id,
            before,
            before + len(itinerary_rows),
            inherit_style="BEFORE",
        )
        if itin_ranges:
            client.values_batch_update(token, spreadsheet_token, itin_ranges)
            itinerary_rows_written = len(itinerary_rows)

    return GenerateResponse(
        success=True,
        document_id=spreadsheet_token,
        file_token=spreadsheet_token,
        document_url=document_url,
        document_name=doc_name,
        template_token=tpl.template_token,
        folder_token=tpl.folder_token,
        output_folder=tpl.output_folder or None,
        replaced_blocks=replaced_cells,
        sheet_rows_written=sheet_rows_written,
        itinerary_rows_written=itinerary_rows_written,
        unresolved_placeholders=unresolved_ph,
        warnings=warnings,
        placeholders=values,
    )


def _generate_docx(
    client: FeishuClient,
    token: str,
    tpl: Any,
    req: GenerateRequest,
    values: Dict[str, str],
    doc_name: str,
) -> Any:
    # Legacy merged body placeholder must stay empty; detail goes to end tables.
    values["活动行程与费用明细"] = ""

    copied = client.copy_file(
        token,
        template_token=tpl.template_token,
        folder_token=tpl.folder_token,
        name=doc_name,
        file_type="docx",
    )
    document_id = str(copied["token"])
    document_url = str(
        copied.get("url") or f"https://feishu.cn/docx/{document_id}"
    )

    blocks = client.list_all_blocks(token, document_id)
    update_requests = build_update_requests(blocks, values)
    if update_requests:
        client.batch_update(token, document_id, update_requests)

    sheet_rows_written = 0
    sheets_appended = 0
    warnings: List[str] = []

    detail_sheets_cfg = getattr(tpl, "detail_sheets", None) or {}
    has_settlement = isinstance(detail_sheets_cfg, dict) and isinstance(
        detail_sheets_cfg.get("settlement"), dict
    )

    if getattr(tpl, "append_detail_sheets", False):
        try:
            sheets_appended = append_contract_detail_sheets(
                client,
                token,
                document_id,
                req.fields,
                detail_sheets_cfg if isinstance(detail_sheets_cfg, dict) else None,
                sheet_rows=req.sheet_rows,
            )
            if has_settlement:
                sheet_rows_written = len(
                    resolve_settlement_rows(
                        sheet_rows=req.sheet_rows, fields=req.fields
                    )
                )
        except FeishuAPIError as exc:
            return _error_response(
                error_code="SHEET_APPEND_FAILED",
                message=f"文末追加电子表格失败: {exc.message}",
                status_code=502,
                details={
                    "template_key": tpl.key,
                    "feishu_error_code": exc.error_code,
                    "feishu_code": exc.feishu_code,
                    **(exc.details or {}),
                },
            )
        except SheetConfigError as exc:
            return _error_response(
                error_code=exc.error_code,
                message=f"文末电子表格配置错误: {exc.message}",
                status_code=400,
                details={"template_key": tpl.key},
            )
        except ValueError as exc:
            return _error_response(
                error_code="SHEET_APPEND_FAILED",
                message=f"文末追加电子表格失败: {exc}",
                status_code=502,
                details={"template_key": tpl.key},
            )

    # Legacy: fill pre-embedded template Sheet (skip when settlement is appended)
    sheet_rows = resolve_sheet_rows(sheet_rows=req.sheet_rows, fields=req.fields)
    if tpl.sheet and sheet_rows and not has_settlement:
        sheet_refs = find_sheet_refs(blocks)
        if not sheet_refs:
            return _error_response(
                error_code="SHEET_BLOCK_NOT_FOUND",
                message="模板配置了 sheet，但复制后的文档中未找到电子表格块(block_type=30)",
                details={"template_key": tpl.key},
            )
        # Use first sheet block (出团计划单约定仅一块预定结算表)
        ref = sheet_refs[0]
        try:
            value_ranges = build_sheet_value_ranges(
                ref["sheet_id"],
                tpl.sheet,
                sheet_rows,
            )
            formula_ranges = build_formula_ranges(
                ref["sheet_id"],
                tpl.sheet,
                len(sheet_rows),
            )
            delete_range = unused_row_delete_range(tpl.sheet, len(sheet_rows))
        except SheetConfigError as exc:
            return _error_response(
                error_code=exc.error_code,
                message=exc.message,
                details={"template_key": tpl.key},
            )
        if value_ranges:
            client.values_batch_update(
                token,
                ref["spreadsheet_token"],
                value_ranges,
            )
            sheet_rows_written = min(
                len(sheet_rows),
                int(tpl.sheet.get("max_rows") or len(sheet_rows)),
            )
        # Write subtotal + total formulas after data values
        if formula_ranges:
            client.values_batch_update(
                token,
                ref["spreadsheet_token"],
                formula_ranges,
            )
        # Drop template padding rows below the new total row
        if delete_range is not None:
            del_start, del_end = delete_range
            client.delete_dimension_range(
                token,
                ref["spreadsheet_token"],
                ref["sheet_id"],
                del_start,
                del_end,
            )
        if len(sheet_refs) > 1:
            warnings.append(
                f"文档含 {len(sheet_refs)} 个 Sheet 块，仅写入第一个"
            )
    elif sheet_rows and not tpl.sheet and not has_settlement:
        warnings.append("请求含 sheet_rows/报价明细条目，但模板未配置 sheet，已跳过表格写入")

    # Re-check unresolved against original blocks with values applied conceptually:
    # keys still in original text that were not provided.
    unresolved = find_unresolved(blocks, values)
    if unresolved and not (req.options and req.options.skip_unresolved_warnings):
        warnings.append(f"文档中仍有未提供值的占位符: {', '.join(unresolved)}")

    return GenerateResponse(
        success=True,
        document_id=document_id,
        file_token=document_id,
        document_url=document_url,
        document_name=doc_name,
        template_token=tpl.template_token,
        folder_token=tpl.folder_token,
        output_folder=tpl.output_folder or None,
        replaced_blocks=len(update_requests),
        sheet_rows_written=sheet_rows_written,
        sheets_appended=sheets_appended,
        unresolved_placeholders=unresolved,
        warnings=warnings,
        placeholders=values,
    )


if __name__ == "__main__":
    import uvicorn

    # Local default: use repo config.yaml when not in Docker
    if not CONFIG_PATH.exists():
        local = Path(__file__).resolve().parent / "config.yaml"
        if local.exists():
            os.environ["FEISHU_FILES_GENERATION_CONFIG_PATH"] = str(local)
            globals()["CONFIG_PATH"] = local

    uvicorn.run(app, host=HOST, port=PORT)
