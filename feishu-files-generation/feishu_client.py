"""Feishu Open API client with rate limiting and retries."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from config_loader import RateLimitConfig

logger = logging.getLogger("feishu-files-generation.feishu")

FEISHU_BASE = "https://open.feishu.cn/open-apis"
RETRYABLE_CODES = {1061045, 99991400}


class FeishuAPIError(Exception):
    """Feishu API returned a non-zero code or HTTP error."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "FEISHU_API_ERROR",
        feishu_code: Optional[int] = None,
        http_status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.feishu_code = feishu_code
        self.http_status = http_status
        self.details = details or {}


class FeishuClient:
    def __init__(
        self,
        *,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        rate_limit: Optional[RateLimitConfig] = None,
        timeout: float = 60.0,
    ):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self.rate_limit = rate_limit or RateLimitConfig()
        self._client = httpx.Client(timeout=timeout)
        self._copy_lock = threading.Lock()
        self._last_write_at = 0.0
        self._last_read_at = 0.0
        self._io_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _throttle(self, kind: str) -> None:
        with self._io_lock:
            now = time.monotonic()
            if kind == "write":
                min_interval = 1.0 / max(self.rate_limit.write_qps, 0.1)
                elapsed = now - self._last_write_at
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
                self._last_write_at = time.monotonic()
            else:
                min_interval = 1.0 / max(self.rate_limit.read_qps, 0.1)
                elapsed = now - self._last_read_at
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
                self._last_read_at = time.monotonic()

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _parse_json(self, resp: httpx.Response) -> Dict[str, Any]:
        try:
            data = resp.json()
        except Exception as exc:
            raise FeishuAPIError(
                f"Invalid JSON from Feishu (HTTP {resp.status_code})",
                error_code="FEISHU_BAD_RESPONSE",
                http_status=resp.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise FeishuAPIError(
                "Feishu response is not an object",
                error_code="FEISHU_BAD_RESPONSE",
                http_status=resp.status_code,
            )
        return data

    def _raise_for_code(self, data: Dict[str, Any], *, action: str) -> None:
        code = data.get("code")
        if code == 0:
            return
        msg = str(data.get("msg") or "unknown error")
        error_code = "FEISHU_API_ERROR"
        if code == 1061004:
            error_code = "FEISHU_FORBIDDEN"
        elif code in RETRYABLE_CODES:
            error_code = "FEISHU_RATE_LIMITED"
        raise FeishuAPIError(
            f"{action} failed: code={code} msg={msg}",
            error_code=error_code,
            feishu_code=int(code) if isinstance(code, int) else None,
            details=data,
        )

    def get_tenant_token(self) -> str:
        if not self.app_id or not self.app_secret:
            raise FeishuAPIError(
                "FEISHU_APP_ID / FEISHU_APP_SECRET not configured and no token provided",
                error_code="MISSING_CREDENTIALS",
            )
        self._throttle("write")
        resp = self._client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = self._parse_json(resp)
        # This endpoint uses top-level tenant_access_token, code may be 0
        if data.get("code", 0) != 0:
            self._raise_for_code(data, action="get_tenant_token")
        token = data.get("tenant_access_token")
        if not token:
            raise FeishuAPIError(
                "tenant_access_token missing in auth response",
                error_code="MISSING_TOKEN",
                details=data,
            )
        return str(token)

    def resolve_token(self, tenant_access_token: Optional[str] = None) -> str:
        token = (tenant_access_token or "").strip()
        if token:
            return token
        return self.get_tenant_token()

    def copy_file(
        self,
        token: str,
        *,
        template_token: str,
        folder_token: str,
        name: str,
        file_type: str = "docx",
    ) -> Dict[str, Any]:
        """Copy a cloud document. Serialized + retried for rate limits."""
        url = f"{FEISHU_BASE}/drive/v1/files/{template_token}/copy"
        body = {
            "name": name,
            "type": file_type,
            "folder_token": folder_token,
        }
        max_retries = max(self.rate_limit.copy_retry_max, 1)
        base_delay = self.rate_limit.copy_retry_delay_ms / 1000.0

        with self._copy_lock:
            last_error: Optional[FeishuAPIError] = None
            for attempt in range(max_retries):
                self._throttle("write")
                resp = self._client.post(url, headers=self._headers(token), json=body)
                data = self._parse_json(resp)
                code = data.get("code")
                if code == 0:
                    file_info = (data.get("data") or {}).get("file") or {}
                    if not file_info.get("token"):
                        raise FeishuAPIError(
                            "copy succeeded but file.token missing",
                            error_code="FEISHU_BAD_RESPONSE",
                            details=data,
                        )
                    return file_info
                if isinstance(code, int) and code in RETRYABLE_CODES and attempt + 1 < max_retries:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "copy_file retryable code=%s attempt=%s sleep=%.2fs",
                        code,
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                    last_error = FeishuAPIError(
                        f"copy_file rate limited: {data.get('msg')}",
                        error_code="FEISHU_RATE_LIMITED",
                        feishu_code=code,
                        details=data,
                    )
                    continue
                self._raise_for_code(data, action="copy_file")
            if last_error:
                raise last_error
            raise FeishuAPIError("copy_file failed after retries", error_code="FEISHU_RATE_LIMITED")

    def list_all_blocks(self, token: str, document_id: str) -> List[Dict[str, Any]]:
        """List all descendant blocks under the document root."""
        items: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            self._throttle("read")
            params: Dict[str, Any] = {
                "document_revision_id": -1,
                "page_size": 500,
                "with_descendants": "true",
            }
            if page_token:
                params["page_token"] = page_token
            url = (
                f"{FEISHU_BASE}/docx/v1/documents/{document_id}"
                f"/blocks/{document_id}/children"
            )
            resp = self._client.get(url, headers=self._headers(token), params=params)
            data = self._parse_json(resp)
            self._raise_for_code(data, action="list_all_blocks")
            payload = data.get("data") or {}
            batch = payload.get("items") or []
            items.extend(batch)
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break
        return items

    def batch_update(
        self,
        token: str,
        document_id: str,
        requests: List[Dict[str, Any]],
    ) -> None:
        """Send update requests in chunks with write throttling."""
        if not requests:
            return
        batch_size = max(int(self.rate_limit.batch_size), 1)
        delay = max(self.rate_limit.batch_delay_ms, 0) / 1000.0
        url = f"{FEISHU_BASE}/docx/v1/documents/{document_id}/blocks/batch_update"

        for i in range(0, len(requests), batch_size):
            chunk = requests[i : i + batch_size]
            self._throttle("write")
            resp = self._client.patch(
                url,
                headers=self._headers(token),
                json={"requests": chunk},
            )
            data = self._parse_json(resp)
            code = data.get("code")
            if code != 0 and isinstance(code, int) and code in RETRYABLE_CODES:
                time.sleep(delay * 2)
                self._throttle("write")
                resp = self._client.patch(
                    url,
                    headers=self._headers(token),
                    json={"requests": chunk},
                )
                data = self._parse_json(resp)
            self._raise_for_code(data, action="batch_update")
            if i + batch_size < len(requests) and delay > 0:
                time.sleep(delay)

    def values_batch_update(
        self,
        token: str,
        spreadsheet_token: str,
        value_ranges: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Write multiple ranges into an embedded / standalone spreadsheet."""
        if not value_ranges:
            return {}
        url = (
            f"{FEISHU_BASE}/sheets/v2/spreadsheets/"
            f"{spreadsheet_token}/values_batch_update"
        )
        self._throttle("write")
        resp = self._client.post(
            url,
            headers=self._headers(token),
            json={"valueRanges": value_ranges},
        )
        data = self._parse_json(resp)
        code = data.get("code")
        if code != 0 and isinstance(code, int) and code in RETRYABLE_CODES:
            time.sleep(max(self.rate_limit.batch_delay_ms, 0) / 1000.0 * 2)
            self._throttle("write")
            resp = self._client.post(
                url,
                headers=self._headers(token),
                json={"valueRanges": value_ranges},
            )
            data = self._parse_json(resp)
        self._raise_for_code(data, action="values_batch_update")
        return data.get("data") or {}
