from fastapi import FastAPI, UploadFile, File, HTTPException
import fitz
import io
import base64
import re
from typing import List, Dict, Any, Set

try:
    import pytesseract
    from PIL import Image
except ImportError:  # pragma: no cover
    pytesseract = None
    Image = None

app = FastAPI(title="PDF Parser Service")


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


def extract_invoice_fields(text: str) -> Dict[str, Any]:
    text = clean_text(text)

    lines = [
        line.strip()
        for line in text.splitlines()
        if line and line.strip()
    ]

    def clean_name(s: str) -> str:
        s = s.strip()
        s = re.sub(r"^(名称[:：]?\s*)", "", s)
        s = re.sub(r"^(购方名称[:：]?\s*)", "", s)
        s = re.sub(r"^(销方名称[:：]?\s*)", "", s)
        return s.strip()

    def looks_like_name(s: str) -> bool:
        if not s:
            return False

        bad_keywords = [
            "发票", "开票", "价税", "合计", "税率", "税额", "金额",
            "项目名称", "规格型号", "下载次数", "备注", "银行账号",
            "地址", "电话", "开户银行", "服务", "包车费", "餐饮费",
            "出行人", "有效身份证件号", "出行日期", "交通工具类型",
            "国家税务总局", "发票监制章",
        ]

        if any(k in s for k in bad_keywords):
            return False

        good_keywords = [
            "有限公司",
            "有限责任公司",
            "股份有限公司",
            "个体工商户",
            "餐厅",
            "饭店",
            "酒店",
            "商行",
            "经营部",
            "服务部",
            "工作室",
            "中心",
            "门市部",
            "店",
            "厂",
            "公司",
            "合作社",
        ]

        return any(k in s for k in good_keywords)

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

    # 3. 提取纳税人识别号候选
    tax_ids = []
    for m in re.finditer(r"\b[0-9A-Z]{15,20}\b", text):
        v = m.group(0)
        if v != invoice_number and v not in tax_ids:
            tax_ids.append(v)

    # 4. 提取名称候选
    name_candidates = []

    name_patterns = [
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,80}有限公司",
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,80}有限责任公司",
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,80}股份有限公司",
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,80}（个体工商户）",
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,80}\(个体工商户\)",
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,50}(?:餐厅|饭店|酒店|商行|经营部|服务部|工作室|中心|门市部|店|厂|合作社)",
    ]

    for pattern in name_patterns:
        for m in re.finditer(pattern, text):
            name = clean_name(m.group(0))

            # 避免把地址、开户行等长句误识别成名称
            if not looks_like_name(name):
                continue

            if name not in name_candidates:
                name_candidates.append(name)

    # 5. 优先根据名称出现顺序判断购销方
    buyer_name = name_candidates[0] if len(name_candidates) >= 1 else ""
    seller_name = name_candidates[1] if len(name_candidates) >= 2 else ""

    # 6. 如果名称候选不足，再根据税号附近行兜底
    if not buyer_name or not seller_name:
        names_near_tax = []

        for idx, line in enumerate(lines):
            if not re.search(r"\b[0-9A-Z]{15,20}\b", line):
                continue

            # 向前找最近的名称行
            found_name = ""
            for j in range(idx - 1, max(-1, idx - 8), -1):
                candidate = clean_name(lines[j])
                if looks_like_name(candidate):
                    found_name = candidate
                    break

            if found_name and found_name not in names_near_tax:
                names_near_tax.append(found_name)

        if not buyer_name and len(names_near_tax) >= 1:
            buyer_name = names_near_tax[0]

        if not seller_name:
            for name in names_near_tax:
                if name != buyer_name:
                    seller_name = name
                    break

    # 7. 防止购销方相同
    if buyer_name and seller_name and buyer_name == seller_name:
        # 如果候选里有第二个不同名称，用第二个不同名称作为销方
        for name in name_candidates:
            if name != buyer_name:
                seller_name = name
                break

        # 如果仍然相同，直接清空销方，交给 n8n 后续失败/AI兜底
        if buyer_name == seller_name:
            seller_name = ""

    # 8. 价税合计：优先取最大 ¥ 金额
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


def render_page_image_base64(page, scale: float = 2.0) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return base64.b64encode(pix.tobytes("png")).decode("ascii")


def page_needs_ocr(text: str, min_chars: int = 40) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return len(compact) < min_chars


def ocr_page(page) -> str:
    if pytesseract is None or Image is None:
        return ""

    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return clean_text(
        pytesseract.image_to_string(image, lang="chi_sim+eng", config="--psm 6")
    )


def extract_page_text(page) -> Dict[str, Any]:
    plain = clean_text(page.get_text("text") or "")
    block = clean_text(blocks_to_text(page))
    words = clean_text(words_to_text(page))
    best_engine, best_text = max(
        [("plain_text", plain), ("block_text", block), ("words_text", words)],
        key=lambda item: len(item[1] or ""),
    )

    used_ocr = False
    if page_needs_ocr(best_text):
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
            stamp_images.append(
                {
                    "page": page_no,
                    "image_base64": render_page_image_base64(doc[page_no - 1]),
                }
            )

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


@app.get("/health")
def health():
    return {"ok": True, "service": "pdf-parser"}


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
    filename = file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    return parse_contract_pdf(data, filename)


@app.post("/parse-contract-invoice")
async def parse_contract_invoice(file: UploadFile = File(...)):
    filename = file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    return parse_contract_pdf(data, filename, mode="invoice")
