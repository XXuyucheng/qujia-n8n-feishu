"""Pydantic request/response models for feishu-files-generation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GenerateOptions(BaseModel):
    dry_run: bool = False
    skip_unresolved_warnings: bool = False


class GenerateRequest(BaseModel):
    """Generate a contract / departure plan from a Feishu docx template.

    Two modes:
    1. Explicit: template_token + folder_token + placeholders
    2. Config-driven: signing_unit + fields (mapped via config.yaml)
    """

    tenant_access_token: Optional[str] = None
    signing_unit: Optional[str] = None
    document_name: Optional[str] = None
    fields: Optional[Dict[str, Any]] = None
    template_token: Optional[str] = None
    folder_token: Optional[str] = None
    placeholders: Optional[Dict[str, str]] = None
    sheet_rows: Optional[List[Dict[str, Any]]] = None
    options: GenerateOptions = Field(default_factory=GenerateOptions)


class ProbeRequest(BaseModel):
    tenant_access_token: Optional[str] = None
    template_token: str


class PlaceholderHit(BaseModel):
    key: str
    block_id: str
    sample: str


class ProbeResponse(BaseModel):
    template_token: str
    placeholders_found: List[PlaceholderHit]
    block_count: int
    text_block_count: int
    sheet_block_count: int = 0
    sheet_tokens: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class GenerateResponse(BaseModel):
    success: bool
    document_id: Optional[str] = None
    file_token: Optional[str] = None
    document_url: Optional[str] = None
    document_name: Optional[str] = None
    template_token: Optional[str] = None
    replaced_blocks: int = 0
    sheet_rows_written: int = 0
    unresolved_placeholders: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    placeholders: Optional[Dict[str, str]] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class PreviewResponse(BaseModel):
    success: bool = True
    dry_run: bool = True
    document_name: Optional[str] = None
    template_token: Optional[str] = None
    folder_token: Optional[str] = None
    placeholders: Dict[str, str] = Field(default_factory=dict)
    missing_required: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
