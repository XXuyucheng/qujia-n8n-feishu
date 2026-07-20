"""Feishu Open API client for chat-app identity (IM + bitable)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("feishu-bot.feishu")

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        feishu_code: Optional[int] = None,
        http_status: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.feishu_code = feishu_code
        self.http_status = http_status
        self.details = details or {}


class FeishuClient:
    def __init__(
        self,
        *,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.app_id = app_id or os.getenv("FEISHU_CHAT_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_CHAT_APP_SECRET", "")
        self._client = httpx.Client(timeout=timeout)
        self._token: Optional[str] = None
        self._token_expire_at = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_tenant_access_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        if not self.app_id or not self.app_secret:
            raise FeishuAPIError("FEISHU_CHAT_APP_ID/SECRET missing")
        resp = self._client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuAPIError(
                f"token failed: {data.get('msg')}",
                feishu_code=data.get("code"),
            )
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200))
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _raise(self, data: Dict[str, Any], action: str) -> None:
        if data.get("code") == 0:
            return
        raise FeishuAPIError(
            f"{action} failed: code={data.get('code')} msg={data.get('msg')}",
            feishu_code=data.get("code") if isinstance(data.get("code"), int) else None,
            details=data,
        )

    def reply_text(self, message_id: str, text: str) -> None:
        resp = self._client.post(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply",
            headers=self._headers(),
            json={
                "msg_type": "text",
                "content": json_dumps({"text": text}),
            },
        )
        data = resp.json()
        self._raise(data, "reply")

    def send_text(self, receive_id: str, text: str, *, receive_id_type: str = "chat_id") -> None:
        resp = self._client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers=self._headers(),
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json_dumps({"text": text}),
            },
        )
        data = resp.json()
        self._raise(data, "send")

    def download_message_resource(
        self, message_id: str, file_key: str, *, resource_type: str = "image"
    ) -> bytes:
        resp = self._client.get(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {self.get_tenant_access_token()}"},
            params={"type": resource_type},
        )
        if resp.status_code != 200:
            raise FeishuAPIError(
                f"download resource failed HTTP {resp.status_code}",
                http_status=resp.status_code,
            )
        # may be JSON error
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            data = resp.json()
            self._raise(data, "download resource")
        return resp.content

    def upload_bitable_media(
        self,
        *,
        file_name: str,
        content: bytes,
        parent_node: str,
        parent_type: str = "bitable_file",
    ) -> str:
        """Upload media for bitable attachment field; returns file_token.

        Attachment fields need parent_type=bitable_file plus extra.drive_route_token
        (same as app_token). See Feishu media introduction.
        """
        token = self.get_tenant_access_token()
        extra = json_dumps({"drive_route_token": parent_node})
        resp = self._client.post(
            f"{FEISHU_BASE}/drive/v1/medias/upload_all",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "file_name": file_name,
                "parent_type": parent_type,
                "parent_node": parent_node,
                "size": str(len(content)),
                "extra": extra,
            },
            files={"file": (file_name, content)},
        )
        data = resp.json()
        self._raise(data, "upload media")
        file_token = (data.get("data") or {}).get("file_token")
        if not file_token:
            raise FeishuAPIError("upload media missing file_token", details=data)
        return str(file_token)

    def search_records(
        self,
        *,
        app_token: str,
        table_id: str,
        field_name: str,
        value: str,
        page_size: int = 20,
    ) -> List[Dict[str, Any]]:
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            headers=self._headers(),
            json={
                "page_size": page_size,
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_name,
                            "operator": "is",
                            "value": [value],
                        }
                    ],
                },
            },
        )
        data = resp.json()
        self._raise(data, "search records")
        return list((data.get("data") or {}).get("items") or [])

    def create_record(
        self, *, app_token: str, table_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=self._headers(),
            json={"fields": fields},
        )
        data = resp.json()
        self._raise(data, "create record")
        return (data.get("data") or {}).get("record") or {}

    def update_record(
        self,
        *,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        resp = self._client.put(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            headers=self._headers(),
            json={"fields": fields},
        )
        data = resp.json()
        self._raise(data, "update record")
        return (data.get("data") or {}).get("record") or {}


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
