# PDF Parser

PDF 文本解析服务：支持发票字段提取、合同页码裁剪、扫描件 OCR，以及签章页图片导出。供 n8n 工作流通过 Docker 内网调用。

## 启动

```bash
docker compose build pdf-parser
docker compose up -d pdf-parser
```

健康检查：

```bash
curl -s http://localhost:8000/health
# 期望 ocr.available=true；若为 false，说明镜像里没有 tesseract，需无缓存重建：
# docker compose build --no-cache pdf-parser && docker compose up -d pdf-parser
```

n8n 容器内调用地址：

```text
http://pdf-parser:8000
```

### 常见问题：合同解析 500 / `TesseractNotFoundError`

日志出现 `tesseract is not installed or it's not in your PATH` 时，容器镜像未装上系统包。请在服务器拉最新代码后：

```bash
docker compose build --no-cache pdf-parser
docker compose up -d pdf-parser
docker exec pdf-parser tesseract --version
curl -s http://localhost:8000/health
```

即使 OCR 不可用，新版也会跳过 OCR 继续返回文本/签章图，不再因缺 tesseract 整请求 500。

## 技术栈

- Python 3.12 + FastAPI + uvicorn
- PyMuPDF（文本提取、页面渲染）
- Tesseract OCR（`chi_sim+eng`，扫描件兜底）

## API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/parse` | 全量解析 PDF，提取发票字段 |
| `POST` | `/parse-contract` | 合同 PDF：按页码规则抽取文本 |
| `POST` | `/parse-contract-invoice` | 发票申请表合同：额外抽取签章页图片 |

所有 `POST` 接口均使用 `multipart/form-data`，字段名 `file`，上传 PDF 二进制。

---

### `GET /health`

```bash
curl http://localhost:8000/health
```

```json
{ "ok": true, "service": "pdf-parser" }
```

---

### `POST /parse` — 发票解析

适用场景：电子/扫描发票 PDF，提取结构化字段供 n8n 后续核验。

```bash
curl -X POST http://localhost:8000/parse \
  -F "file=@invoice.pdf"
```

#### 输出结构

```json
{
  "success": true,
  "filename": "invoice.pdf",
  "page_count": 1,
  "engine": "block_text",
  "text_length": 2048,
  "fields": {
    "发票号码": "25312000000123456789",
    "开票日期": "2026-06-25",
    "购方名称": "某某有限公司",
    "销方名称": "某某餐厅",
    "价税合计": "80000.00",
    "公司名称候选": ["某某有限公司", "某某餐厅"],
    "纳税人识别号候选": ["91310000MA1FL2XXXX", "91310000MA1FL3YYYY"],
    "开户银行候选": ["台州银行股份有限公司杭州滨江支行"],
    "购销配对来源": "tax_pair"
  },
  "text": "全文 OCR/文本层内容...",
  "debug": {
    "plain_text_length": 1800,
    "block_text_length": 2048,
    "words_text_length": 1950,
    "plain_text_preview": "...",
    "block_text_preview": "...",
    "words_text_preview": "..."
  }
}
```

#### 字段提取规则（`fields`）

| 字段 | 规则 |
| --- | --- |
| `发票号码` | 优先匹配 20 位数字（新版电子发票） |
| `开票日期` | 匹配 `YYYY年M月D日` 或 `YYYY-MM-DD`，归一化为 `YYYY-MM-DD` |
| `购方名称` / `销方名称` | 先剔除 `开户银行`/`银行账号` 片段；优先按「名称行 + 纳税人识别号」成对提取（先购后销）；再回落 scrubbed 正则候选。开户行中的银行名不会当作购销方，但带税号的银行工会/含「银行」的科技公司等真实客商会保留 |
| `价税合计` | 优先取 `（小写）` / `价税合计` 邻近的 `¥` 金额；否则取全文最大 `¥`/`￥` 金额，保留两位小数 |
| `公司名称候选` | 过滤开户行污染后的名称列表，按出现顺序 |
| `纳税人识别号候选` | 15–20 位字母数字串，排除与发票号码相同的值 |
| `开户银行候选` | 从 `销方/购方开户银行:` 行解析出的开户行文本（debug） |
| `购销配对来源` | `tax_pair` / `mixed` / `regex`（debug） |

文本引擎：对每一页分别用 `plain_text`、`block_text`、`words_text` 三种 PyMuPDF 提取方式，取**总字符数最多**的结果作为最终 `text` 和字段提取来源。

---

### `POST /parse-contract` — 合同文本抽取

适用场景：订单合同 PDF，只抽取关键页文本，字段识别由 n8n 侧 AI Agent 完成。

```bash
curl -X POST http://localhost:8000/parse-contract \
  -F "file=@contract.pdf"
```

#### 页码选择规则（`selected_pages`）

固定取**前 3 页** + **倒数两页**，去重后升序（1-based）：

| 总页数 | 抽取页码 |
| ---: | --- |
| 1 | 1 |
| 2 | 1, 2 |
| 3 | 1, 2, 3 |
| 4 | 1, 2, 3, 4 |
| 5 | 1, 2, 3, 4, 5 |
| 6 | 1, 2, 3, 5, 6 |
| n (n≥6) | 1, 2, 3, n-1, n |

业务假设：前几页含活动名称、日期、人数、付款条款等基本信息；倒数两页含费用汇总与签章。

#### 输出结构

```json
{
  "success": true,
  "filename": "contract.pdf",
  "mode": "default",
  "page_count": 5,
  "selected_pages": [1, 2, 3, 4, 5],
  "stamp_pages": [],
  "pages": [
    {
      "page": 2,
      "text": "第 2 页 OCR/文本内容...",
      "engine": "ocr",
      "used_ocr": true
    }
  ],
  "stamp_images": []
}
```

| 字段 | 说明 |
| --- | --- |
| `pages[].page` | 页码（1-based） |
| `pages[].text` | 该页提取文本 |
| `pages[].engine` | 使用的引擎：`plain_text` / `block_text` / `words_text` / `ocr` |
| `pages[].used_ocr` | 是否触发 OCR 兜底 |

n8n 侧通常将 `pages` 拼接为 `pageText` 后送入 LLM，期望 AI 返回：

```json
{
  "活动名称": "",
  "活动日期": "YYYY-MM-DD",
  "活动人数": "",
  "活动总费用": "",
  "付款比例和方式": "",
  "说明": ""
}
```

---

### `POST /parse-contract-invoice` — 发票申请表合同

适用场景：飞书发票申请表「合同」附件识别（工作流 `files/合同扫描识别.json`）。

```bash
curl -X POST http://localhost:8000/parse-contract-invoice \
  -F "file=@contract.pdf"
```

#### 页码选择规则

与 `/parse-contract` 相同：**前 3 页** + **倒数两页**（文本 OCR）；签章页单独渲染为图片：

| 总页数 | 文本页 `selected_pages` | 签章页 `stamp_pages` |
| ---: | --- | --- |
| 1 | 1 | 1 |
| 2 | 1, 2 | 1, 2 |
| 3 | 1, 2, 3 | 2, 3 |
| 4 | 1, 2, 3, 4 | 3, 4 |
| 5 | 1, 2, 3, 4, 5 | 4, 5 |
| 6 | 1, 2, 3, 5, 6 | 5, 6 |
| n (n≥6) | 1, 2, 3, n-1, n | n-1, n |

#### 输出结构

与 `/parse-contract` 相同，额外包含：

```json
{
  "mode": "invoice",
  "stamp_pages": [4, 5],
  "stamp_images": [
    {
      "page": 4,
      "image_base64": "iVBORw0KGgo..."
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `stamp_pages` | 签章页页码列表（倒数两页） |
| `stamp_images[].page` | 页码 |
| `stamp_images[].image_base64` | 签章页图片 Base64（默认 JPEG，约 1.5x 渲染），供多模态模型判断甲乙方盖章 |

---

## OCR 策略

每页处理顺序：

1. PyMuPDF 三种文本引擎（`plain_text` / `block_text` / `words_text`），取字符数最多的结果
2. 若有效字符 `< 40`，判定为扫描件，渲染 2x 图片
3. Tesseract `chi_sim+eng`（`--psm 6`）OCR
4. 若 OCR 结果更长，则替换为 OCR 文本

扫描件 PDF（如夸克扫描王导出）通常无文本层，会自动走 OCR 路径。

## 错误响应

| HTTP 状态码 | 场景 |
| --- | --- |
| `400` | 非 PDF 文件、空文件、PDF 无法打开 |

```json
{ "detail": "Only PDF files are supported" }
```

## 部署说明

- 镜像内置 Tesseract + `tesseract-ocr-chi-sim`，修改 OCR 相关代码后需重建镜像：

  ```bash
  docker compose build pdf-parser
  docker compose up -d pdf-parser
  ```

- 国内构建可设置基础镜像（见 `.env.example`）：

  ```bash
  PDF_PARSER_BASE_IMAGE=docker.1ms.run/library/python:3.12-slim
  ```

- 不建议将 `8000` 端口直接暴露公网；仅 n8n 等内网服务调用。

## 相关文件

| 文件 | 说明 |
| --- | --- |
| `app.py` | 服务实现 |
| `Dockerfile` | 容器构建（含 Tesseract） |
| `files/合同扫描识别.json` | 发票合同识别 n8n 工作流 |
| `合同扫描识别工作流.md` | 工作流设计文档 |
