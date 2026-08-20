"""WeChat personal bill parse service for n8n."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pyzipper
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from parser import BillParseError, load_category_config, parse_wechat_file

CONFIG_PATH = Path(os.getenv("BILL_PARSER_CONFIG_PATH", "/app/config.yaml"))
HOST = os.getenv("BILL_PARSER_HOST", "0.0.0.0")
PORT = int(os.getenv("BILL_PARSER_PORT", "8050"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SUPPORTED_PLATFORMS = {"wechat"}
BILL_SUFFIXES = (".csv", ".xlsx", ".xls")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [bill-parser] %(message)s",
)
logger = logging.getLogger("bill-parser")

app = FastAPI(title="Bill Parser Service")


def _category_config() -> dict:
    return load_category_config(CONFIG_PATH)


def _looks_like_zip(filename: str, data: bytes) -> bool:
    lower = (filename or "").lower()
    if lower.endswith((".csv", ".xlsx", ".xls")):
        return False
    return lower.endswith(".zip") or data.startswith(b"PK")


def unzip_bill(data: bytes, password: str) -> tuple[bytes, str]:
    try:
        archive = pyzipper.AESZipFile(io.BytesIO(data))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"无法打开 ZIP: {exc}") from exc

    if password:
        archive.setpassword(password.encode("utf-8"))

    names = [
        name
        for name in archive.namelist()
        if not name.endswith("/")
        and "__MACOSX" not in name
        and name.lower().endswith(BILL_SUFFIXES)
    ]
    if not names:
        raise HTTPException(status_code=400, detail="ZIP 内没有 CSV/XLSX 账单文件")

    name = names[0]
    try:
        return archive.read(name), Path(name).name
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=f"ZIP 解压失败，请检查密码: {exc}") from exc
    except Exception as exc:
        message = str(exc).lower()
        if "password" in message or "bad password" in message:
            raise HTTPException(status_code=400, detail="ZIP 解压失败，请检查密码") from exc
        raise HTTPException(status_code=400, detail=f"ZIP 解压失败: {exc}") from exc


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "bill-parser"}


@app.post("/parse")
async def parse_bill(
    file: UploadFile = File(...),
    password: str = Form(""),
    platform: str = Form("wechat"),
) -> dict:
    platform_key = (platform or "wechat").strip().lower()
    if platform_key not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"暂不支持平台: {platform}")

    filename = file.filename or "bill.csv"
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")

    inner_name = filename
    if _looks_like_zip(filename, data):
        if not (password or "").strip():
            # 未加密 ZIP 也允许空密码；加密包会在解压时报错。
            logger.info("ZIP uploaded without password: %s", filename)
        data, inner_name = unzip_bill(data, (password or "").strip())

    try:
        parsed = parse_wechat_file(data, inner_name, _category_config())
    except BillParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "parsed %s platform=%s count=%s skipped=%s",
        inner_name,
        platform_key,
        parsed["stats"]["count"],
        parsed["stats"]["skipped"],
    )
    return {
        "ok": True,
        "platform": platform_key,
        "filename": inner_name,
        **parsed,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
