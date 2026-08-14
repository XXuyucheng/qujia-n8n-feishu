from fastapi import FastAPI, UploadFile, File, HTTPException
import fitz
import io
import base64
import logging
import re
import shutil
from typing import List, Dict, Any, Set

try:
    import pytesseract
    from PIL import Image
except ImportError:  # pragma: no cover
    pytesseract = None
    Image = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [pdf-parser] %(message)s",
)
logger = logging.getLogger("pdf-parser")

app = FastAPI(title="PDF Parser Service")

# 签章页渲染：优先控制体积，避免大页 2x PNG 撑爆内存导致裸 500
STAMP_RENDER_SCALES = (1.5, 1.0)
STAMP_JPEG_QUALITY = 75
STAMP_MAX_DECODED_BYTES = 10 * 1024 * 1024

_TESSERACT_BIN = shutil.which("tesseract")
TESSERACT_AVAILABLE = bool(pytesseract is not None and Image is not None and _TESSERACT_BIN)
if not TESSERACT_AVAILABLE:
    logger.warning(
        "OCR disabled: tesseract binary missing or pytesseract/Pillow unavailable "
        "(which=%s, pytesseract=%s, Pillow=%s). Text extraction will skip OCR.",
        _TESSERACT_BIN,
        pytesseract is not None,
        Image is not None,
    )
else:
    logger.info("OCR enabled: tesseract at %s", _TESSERACT_BIN)


def normalize_date(value: str) -> str:
    if not value:
        return ""
    m = re.search(r"(\d{4})年?\s*(\d{1,2})月?\s*(\d{1,2})日?", value)
    if not m:
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"


def normalize_amount(value: str) -> str:
    if not value:
        return ""
    value = value.replace(",", "").replace("，", "")
    m = re.search(r"\d+(?:\.\d+)?", value)
    if not m:
        return ""
    return f"{float(m.group(0)):.2f}"


def clean_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text.replace("\x00", ""))).strip()


def blocks_to_text(page) -> str:
    blocks = page.get_text("blocks") or []
    rows = []

    for b in blocks:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, txt = b[:5]
        txt = clean_text(str(txt))
        if txt:
            rows.append({
                "x": float(x0),
                "y": float(y0),
                "text": txt,
            })

    rows.sort(key=lambda r: (round(r["y"] / 3) * 3, r["x"]))
    return "\n".join(r["text"] for r in rows)


def words_to_text(page) -> str:
    words = page.get_text("words") or []
    items = []

    for w in words:
        if len(w) < 5:
            continue
        x0, y0, x1, y1, word = w[:5]
        word = str(word).strip()
        if word:
            items.append({
                "x": float(x0),
                "y": float(y0),
                "word": word,
            })

    items.sort(key=lambda r: (round(r["y"] / 3) * 3, r["x"]))

    lines = []
    for item in items:
        if not lines or abs(lines[-1]["y"] - item["y"]) > 3:
            lines.append({"y": item["y"], "parts": [item]})
        else:
            lines[-1]["parts"].append(item)

    out = []
    for line in lines:
        line["parts"].sort(key=lambda r: r["x"])
        out.append(" ".join(p["word"] for p in line["parts"]))

    return "\n".join(out)


ACCOUNT_BANK_RE = re.compile(
    r"(?:销方|购方)?开户银行\s*[:：]\s*([^;\n；]+)"
    r"(?:\s*[;；]?\s*银行账号\s*[:：]\s*\d+)?"
)

NAME_SUFFIX_RE = re.compile(
    r"(?:"
    r"有限责任公司|股份有限公司|有限公司|"
    r"（个体工商户）|\(个体工商户\)|"
    r"机关工会委员会|工会委员会|委员会|工会|"
    r"研究院|大学|幼儿园|"
    r"餐厅|饭店|酒店|商行|经营部|服务部|工作室|中心|门市部|"
    r"食品店|店|厂|合作社|分公司|公司"
    r")$"
)

NAME_PATTERN_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,80}"
    r"(?:"
    r"有限责任公司|股份有限公司|有限公司|"
    r"（个体工商户）|\(个体工商户\)|"
    r"机关工会委员会|工会委员会|委员会|工会|"
    r"研究院|大学|幼儿园|"
    r"餐厅|饭店|酒店|商行|经营部|服务部|工作室|中心|门市部|"
    r"食品店|店|厂|合作社|分公司"
    r")"
)

TAX_ID_RE = re.compile(r"\b[0-9A-Z]{15,20}\b")


def clean_name(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^(名称[:：]?\s*)", "", s)
    s = re.sub(r"^(购方名称[:：]?\s*)", "", s)
    s = re.sub(r"^(销方名称[:：]?\s*)", "", s)
    return s.strip()


def looks_like_name(s: str) -> bool:
    if not s or len(s) < 2 or len(s) > 80:
        return False

    bad_keywords = [
        "发票", "开票", "价税", "合计", "税率", "税额", "金额",
        "项目名称", "规格型号", "下载次数", "备注", "银行账号",
        "地址", "电话", "开户银行", "包车费", "餐饮费",
        "出行人", "有效身份证件号", "出行日期", "交通工具类型",
        "国家税务总局", "发票监制章", "统一社会信用代码", "纳税人识别号",
        "规格型号", "征收率",
    ]
    if any(k in s for k in bad_keywords):
        return False

    return bool(NAME_SUFFIX_RE.search(s)) or any(
        k in s
        for k in (
            "有限公司", "股份有限公司", "有限责任公司", "个体工商户",
            "工会", "委员会", "研究院", "大学", "幼儿园",
            "餐厅", "饭店", "酒店", "商行", "经营部", "服务部",
            "工作室", "中心", "门市部", "食品店", "店", "厂",
            "公司", "合作社", "分公司",
        )
    )


def extract_account_banks(text: str) -> List[str]:
    banks: List[str] = []
    for m in ACCOUNT_BANK_RE.finditer(text):
        value = m.group(1).strip()
        if value and value not in banks:
            banks.append(value)
    return banks


def scrub_account_bank_spans(text: str) -> str:
    return ACCOUNT_BANK_RE.sub("\n", text)


def matches_account_bank(name: str, account_banks: List[str]) -> bool:
    """True when name is (only) the bank entity from an 开户银行 line."""
    if not name or not account_banks:
        return False
    for bank in account_banks:
        if name == bank or name in bank or bank.startswith(name):
            # Keep real customers whose full legal name only overlaps loosely
            # (e.g. 佑银行科技) — require bank-corp shape for soft exclude.
            if re.search(r"银行股份有限公司$", name):
                return True
            if re.search(r"银行.*(?:支行|分行)$", name):
                return True
            if name == bank:
                return True
    return False


def extract_invoice_fields(text: str) -> Dict[str, Any]:
    text = clean_text(text)
    account_banks = extract_account_banks(text)
    scrubbed = scrub_account_bank_spans(text)

    lines = [
        line.strip()
        for line in scrubbed.splitlines()
        if line and line.strip()
    ]

    # 1. 发票号码：新版电子发票常见 20 位号码
    invoice_number = ""
    m = re.search(r"\b\d{20}\b", text)
    if m:
        invoice_number = m.group(0)

    # 2. 开票日期
    invoice_date = ""
    date_match = re.search(r"\d{4}年\s*\d{1,2}月\s*\d{1,2}日", text)
    if not date_match:
        date_match = re.search(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", text)
    if date_match:
        invoice_date = normalize_date(date_match.group(0))

    # 3. 纳税人识别号候选（排除发票号码）
    tax_ids: List[str] = []
    for m in TAX_ID_RE.finditer(scrubbed):
        v = m.group(0)
        if v != invoice_number and v not in tax_ids:
            tax_ids.append(v)

    # 4. Tokenize：名称行 / 税号行（优先税号配对）
    tokens: List[tuple] = []
    for line in lines:
        inline = re.match(
            r"^(.+?)\s+([0-9A-Z]{15,20})$",
            line,
        )
        if inline and looks_like_name(clean_name(inline.group(1))):
            tokens.append(("name", clean_name(inline.group(1))))
            tokens.append(("tax", inline.group(2)))
            continue

        if re.fullmatch(r"[0-9A-Z]{15,20}", line):
            if line == invoice_number:
                tokens.append(("inv", line))
            else:
                tokens.append(("tax", line))
            continue

        candidate = clean_name(line)
        if looks_like_name(candidate):
            tokens.append(("name", candidate))

    paired_names: List[str] = []
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "name":
            j = i + 1
            while j < len(tokens) and tokens[j][0] not in ("tax", "name"):
                j += 1
            if j < len(tokens) and tokens[j][0] == "tax":
                if value not in paired_names:
                    paired_names.append(value)
                i = j + 1
                continue
            if value not in paired_names:
                paired_names.append(value)
        i += 1

    # 名称紧邻税号的反向扫描兜底（版式把税号放在名称下一行）
    if len(paired_names) < 2:
        for idx, line in enumerate(lines):
            if not re.fullmatch(r"[0-9A-Z]{15,20}", line):
                continue
            if line == invoice_number:
                continue
            for j in range(idx - 1, max(-1, idx - 5), -1):
                candidate = clean_name(lines[j])
                if looks_like_name(candidate) and candidate not in paired_names:
                    paired_names.append(candidate)
                    break

    # 5. 正则候选（仅在已剔除开户行的文本上）
    regex_candidates: List[str] = []
    for m in NAME_PATTERN_RE.finditer(scrubbed):
        name = clean_name(m.group(0))
        if not looks_like_name(name):
            continue
        if matches_account_bank(name, account_banks):
            continue
        if name not in regex_candidates:
            regex_candidates.append(name)

    # 合并：税号配对优先，再补正则候选
    name_candidates: List[str] = []
    for name in paired_names + regex_candidates:
        if matches_account_bank(name, account_banks):
            # 税号配对出的主体即使含「银行」也保留（真实客商）
            if name not in paired_names:
                continue
            # 纯开户行短名（台州银行股份有限公司）若同时出现在开户行值中且
            # 没有独立成对税号以外的证据——paired 里若仅因开户行残留则已 scrub
            if re.search(r"银行股份有限公司$", name) and any(
                name in b for b in account_banks
            ):
                # 若该名后面没有专属税号配对（只靠正则扫到），上面已 continue；
                # paired 路径：仅当它真的跟在税号前才保留。建设银行工会全称不含
                # 「银行股份有限公司$」结尾（以委员会结尾），可通过。
                if not name.endswith("委员会") and "工会" not in name:
                    # 检查 tokens 是否 name->tax
                    has_tax_pair = False
                    for t_i, (k, v) in enumerate(tokens):
                        if k == "name" and v == name:
                            if t_i + 1 < len(tokens) and tokens[t_i + 1][0] == "tax":
                                has_tax_pair = True
                                break
                    if not has_tax_pair:
                        continue
        if name not in name_candidates:
            name_candidates.append(name)

    party_source = "tax_pair" if len(paired_names) >= 2 else (
        "mixed" if paired_names else "regex"
    )

    buyer_name = name_candidates[0] if len(name_candidates) >= 1 else ""
    seller_name = name_candidates[1] if len(name_candidates) >= 2 else ""

    if buyer_name and seller_name and buyer_name == seller_name:
        for name in name_candidates:
            if name != buyer_name:
                seller_name = name
                break
        if buyer_name == seller_name:
            seller_name = ""

    # 6. 价税合计：优先（小写）/ 价税合计 邻近金额，否则取最大 ¥
    total_amount = ""
    preferential = re.search(
        r"(?:（小写）|\(小写\)|价税合计[^\n]{0,30})[^\n¥￥]{0,20}[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)",
        text,
    )
    if preferential:
        total_amount = f"{float(preferential.group(1)):.2f}"
    else:
        amounts = []
        for m in re.finditer(r"[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)", text):
            try:
                amounts.append(float(m.group(1)))
            except Exception:
                pass
        total_amount = f"{max(amounts):.2f}" if amounts else ""

    return {
        "发票号码": invoice_number,
        "开票日期": invoice_date,
        "购方名称": buyer_name,
        "销方名称": seller_name,
        "价税合计": total_amount,
        "公司名称候选": name_candidates,
        "纳税人识别号候选": tax_ids,
        "开户银行候选": account_banks,
        "购销配对来源": party_source,
    }


def select_contract_pages(page_count: int) -> List[int]:
    """Return 1-based page numbers: first 3 pages + last 2 pages (deduplicated)."""
    if page_count <= 0:
        return []
    pages: Set[int] = set(range(1, min(3, page_count) + 1))
    if page_count == 1:
        return [1]
    pages.add(page_count - 1)
    pages.add(page_count)
    return sorted(p for p in pages if 1 <= p <= page_count)


def select_invoice_contract_pages(page_count: int) -> List[int]:
    """Invoice contract flow: same as default — first 3 pages + last 2 pages."""
    return select_contract_pages(page_count)


def select_stamp_pages(page_count: int) -> List[int]:
    if page_count <= 0:
        return []
    if page_count == 1:
        return [1]
    return [page_count - 1, page_count]


def render_page_image_base64(page, scale: float = 1.5) -> str:
    """Render page to JPEG base64 (smaller than PNG), raise if still too large."""
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    try:
        if Image is not None:
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=STAMP_JPEG_QUALITY, optimize=True)
            raw = buf.getvalue()
        else:
            # PyMuPDF jpg fallback
            raw = pix.tobytes("jpg")
    finally:
        pix = None
    if len(raw) > STAMP_MAX_DECODED_BYTES:
        raise ValueError(
            f"stamp image too large after render: {len(raw)} bytes at scale={scale}"
        )
    return base64.b64encode(raw).decode("ascii")


def render_stamp_image_safe(page, page_no: int) -> Dict[str, Any]:
    last_error = ""
    for scale in STAMP_RENDER_SCALES:
        try:
            return {
                "page": page_no,
                "image_base64": render_page_image_base64(page, scale=scale),
                "format": "jpeg",
                "scale": scale,
            }
        except Exception as exc:  # noqa: BLE001 — keep parsing alive
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("stamp render failed page=%s scale=%s err=%s", page_no, scale, last_error)
    return {
        "page": page_no,
        "image_base64": "",
        "format": "jpeg",
        "scale": None,
        "error": last_error or "stamp render failed",
    }


def page_needs_ocr(text: str, min_chars: int = 40) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return len(compact) < min_chars


def ocr_page(page) -> str:
    # 缺二进制时绝不能抛 TesseractNotFoundError，否则整份合同解析 500
    if not TESSERACT_AVAILABLE:
        return ""

    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        return clean_text(
            pytesseract.image_to_string(image, lang="chi_sim+eng", config="--psm 6")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr_page failed: %s", exc)
        return ""


def extract_page_text(page) -> Dict[str, Any]:
    plain = clean_text(page.get_text("text") or "")
    block = clean_text(blocks_to_text(page))
    words = clean_text(words_to_text(page))
    best_engine, best_text = max(
        [("plain_text", plain), ("block_text", block), ("words_text", words)],
        key=lambda item: len(item[1] or ""),
    )

    used_ocr = False
    if page_needs_ocr(best_text) and TESSERACT_AVAILABLE:
        ocr_text = ocr_page(page)
        if len(re.sub(r"\s+", "", ocr_text)) > len(re.sub(r"\s+", "", best_text)):
            best_engine = "ocr"
            best_text = ocr_text
            used_ocr = True

    return {
        "engine": best_engine,
        "text": best_text,
        "used_ocr": used_ocr,
        "text_length": len(best_text or ""),
    }


def parse_contract_pdf(data: bytes, filename: str = "", mode: str = "default") -> Dict[str, Any]:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot open PDF: {exc}") from exc

    try:
        page_count = len(doc)
        if mode == "invoice":
            selected_pages = select_invoice_contract_pages(page_count)
            stamp_pages = select_stamp_pages(page_count)
        else:
            selected_pages = select_contract_pages(page_count)
            stamp_pages = []

        page_results: List[Dict[str, Any]] = []

        for page_no in selected_pages:
            page = doc[page_no - 1]
            page_info = extract_page_text(page)
            page_results.append(
                {
                    "page": page_no,
                    "text": page_info["text"],
                    "engine": page_info["engine"],
                    "used_ocr": page_info["used_ocr"],
                }
            )

        stamp_images: List[Dict[str, Any]] = []
        if mode == "invoice":
            for page_no in stamp_pages:
                stamp_images.append(render_stamp_image_safe(doc[page_no - 1], page_no))

        return {
            "success": True,
            "filename": filename,
            "mode": mode,
            "page_count": page_count,
            "selected_pages": selected_pages,
            "stamp_pages": stamp_pages,
            "pages": page_results,
            "stamp_images": stamp_images,
        }
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass


async def _read_pdf_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = file.filename or "upload.pdf"
    # n8n 有时不带 .pdf 后缀，按内容仍尝试解析
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) < 5 or not data.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400,
            detail=f"Not a PDF (filename={filename!r}, bytes={len(data)})",
        )
    return filename, data


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "pdf-parser",
        "ocr": {
            "available": TESSERACT_AVAILABLE,
            "tesseract_bin": _TESSERACT_BIN,
        },
    }


@app.post("/parse")
async def parse_pdf(file: UploadFile = File(...)):
    filename = file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    data = await file.read()

    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot open PDF: {str(e)}")

    plain_pages: List[str] = []
    block_pages: List[str] = []
    word_pages: List[str] = []

    for i, page in enumerate(doc, start=1):
        plain = clean_text(page.get_text("text") or "")
        block = clean_text(blocks_to_text(page))
        words = clean_text(words_to_text(page))

        plain_pages.append(f"--- 第 {i} 页 ---\n{plain}")
        block_pages.append(f"--- 第 {i} 页 ---\n{block}")
        word_pages.append(f"--- 第 {i} 页 ---\n{words}")

    plain_text = clean_text("\n\n".join(plain_pages))
    block_text = clean_text("\n\n".join(block_pages))
    words_text = clean_text("\n\n".join(word_pages))

    candidates = [
        ("plain_text", plain_text),
        ("block_text", block_text),
        ("words_text", words_text),
    ]

    best_engine, best_text = max(candidates, key=lambda x: len(x[1] or ""))

    fields = extract_invoice_fields(best_text)

    return {
        "success": True,
        "filename": filename,
        "page_count": len(doc),
        "engine": best_engine,
        "text_length": len(best_text),
        "fields": fields,
        "text": best_text,
        "debug": {
            "plain_text_length": len(plain_text),
            "block_text_length": len(block_text),
            "words_text_length": len(words_text),
            "plain_text_preview": plain_text[:1000],
            "block_text_preview": block_text[:1000],
            "words_text_preview": words_text[:1000],
        },
    }


@app.post("/parse-contract")
async def parse_contract(file: UploadFile = File(...)):
    try:
        filename, data = await _read_pdf_upload(file)
        logger.info("parse-contract start filename=%s bytes=%s", filename, len(data))
        result = parse_contract_pdf(data, filename)
        logger.info(
            "parse-contract ok pages=%s selected=%s",
            result.get("page_count"),
            result.get("selected_pages"),
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("parse-contract failed")
        raise HTTPException(
            status_code=500,
            detail=f"parse-contract failed: {type(exc).__name__}: {exc}",
        ) from exc


@app.post("/parse-contract-invoice")
async def parse_contract_invoice(file: UploadFile = File(...)):
    try:
        filename, data = await _read_pdf_upload(file)
        logger.info(
            "parse-contract-invoice start filename=%s bytes=%s", filename, len(data)
        )
        result = parse_contract_pdf(data, filename, mode="invoice")
        stamp_ok = sum(1 for s in result.get("stamp_images") or [] if s.get("image_base64"))
        logger.info(
            "parse-contract-invoice ok pages=%s stamps_ok=%s/%s",
            result.get("page_count"),
            stamp_ok,
            len(result.get("stamp_images") or []),
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("parse-contract-invoice failed")
        raise HTTPException(
            status_code=500,
            detail=f"parse-contract-invoice failed: {type(exc).__name__}: {exc}",
        ) from exc
