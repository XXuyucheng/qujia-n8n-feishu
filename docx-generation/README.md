# docx-generation

本地 `.docx` 模板占位符生成服务。把同一段落里被 Word 拆开的 `{{key}}` 拼回去再替换，覆盖正文、表格单元格、页眉和页脚。正文里含 `{{锚点}}` 的段落可以按请求插一张表；占位符后面的文字和分页符会留下。

趣加旅社云文档合同仍由 `feishu-files-generation`（8030）复制模板并回写链接。本服务不上传飞书，也不替换 8030。

## 端口与环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOCX_GENERATION_HOST` | `0.0.0.0` | 监听地址 |
| `DOCX_GENERATION_PORT` | `8060` | HTTP 端口。8040 已被 `feishu-bot` 占用 |
| `DOCX_TEMPLATES_DIR` | `/app/templates` | 模板目录，compose 里只读挂载 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 健康检查

```bash
docker compose build docx-generation
docker compose up -d docx-generation
curl http://localhost:8060/health
```

```json
{"status":"ok"}
```

容器内调用地址：`http://docx-generation:8060`。

## HTTP 调用

本机：`http://localhost:8060`。同一 compose 网络里：`http://docx-generation:8060`。

`Content-Type` 要包含 `application/json` 或 `multipart/form-data`。其他类型返回 400，`{"error":"unsupported_media_type"}`。

### `GET /health`

没有参数。成功时返回 `{"status":"ok"}`。

### `POST /api/generate`

请求体是 JSON 对象。下面的字段都写在这一层。

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `template` | string | 是 | | 模板目录里的相对路径，必须以 `.docx` 结尾，可以带子目录，例如 `旅行社合同模板.docx`、`合同/旅行社合同模板.docx`。服务会去掉首尾空白。空字符串、绝对路径、路径中含 `..`、后缀不是 `.docx`，返回 `template_path_invalid`。目录里没有这个文件，返回 `template_not_found`。多份模板放进同一目录，调用时改这个字符串。 |
| `placeholders` | object | 是 | | 键是占位符名字，不含花括号。模板里的 `{{甲方名称}}` 对应键 `甲方名称`。值必须是字符串。数字、null、数组、对象会使整个请求返回 `invalid_placeholders`。字符串中的 `\n` 写成 Word 换行。 |
| `required` | string[] | 否 | `[]` | 这些键缺失，或去掉空白后为空，返回 `missing_required`，响应里带 `missing` 数组，不返回 docx。 |
| `empty_optional` | string | 否 | `""` | `placeholders` 里出现了、又不在 `required` 里、去掉空白后为空的值，替换成这个字符串。合同场景传 `"/"`。 |
| `tables` | array | 否 | 省略 | 省略或 `null` 时只做文字替换。每一项在正文里插一张表，然后再做文字替换。 |

没出现在 `placeholders` 里的 `{{key}}` 留在原文中。成功响应若仍有这种键，响应头 `X-Docx-Unresolved` 是这些键组成的 JSON 数组，按 UTF-8 做百分号编码。

文字替换覆盖正文、已有表格单元格、页眉和页脚。同一段落里被 Word 拆开的 `{{key}}` 会先拼回再替换。

#### `tables` 的每一项

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `anchor` | string | 是 | | 花括号里面的名字。模板写 `{{报价}}` 时这里传 `报价`。不能包含 `{` 或 `}`。 |
| `columns` | array | 是 | | 至少一列。 |
| `rows` | array | 否 | `[]` | 每个元素是一个对象，键对应列的 `key`。空数组只生成表头，并去掉占位符。 |
| `header` | boolean | 否 | `true` | `true` 时写出表头行，表头加粗，并在跨页时重复。`false` 时不写表头行。 |
| `borders` | boolean | 否 | `true` | `true` 时六边为单线。`false` 时不写边框。 |
| `empty_cell` | string | 否 | 同 `empty_optional` | 单元格为 null、这一列没有键、或去掉空白后为空时写入的文字。字段省略或值为 null 时，用 `empty_optional`。 |

锚点只在正文段落里找，不进已有表格内部，也不进页眉页脚。段落拼起来的文字里包含 `{{anchor}}` 即命中。找不到返回 `table_anchor_not_found`，响应里带 `anchor`。同一次请求里锚点重复，或正文里同一锚点出现多次，返回 `table_anchor_duplicate`。

占位符删掉之后，这段里还有文字或分页符时，表插在这段前面，其余内容和分页符保留。合同模板里 `{{报价}}` 后面的分页和「附件2」因此还在。这段去掉占位符后没有剩余文字、也没有分页符时，整段换成表。

字体沿用占位段落第一个文字 run 的字体和字号。占位 run 没有东亚字体时，补段落标记上的东亚字体。单元格段落不带首行缩进。表头加粗，数据单元格不加粗。布局为 fixed。

#### `columns` 的每一项

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `key` | string | 是 | | 对应 `rows` 里的字段名。会去掉首尾空白。 |
| `header` | string | 否 | 等于 `key` | 表头显示的文字。 |
| `width` | integer | 否 | 均分 | 正整数，单位 twip（dxa）。布尔值和小数会被拒绝，JSON 里的 `1.0` 也是小数。 |
| `align` | string | 否 | `left` | `left`、`center`、`right`。 |

不传 `width` 的列，按该段落所在节的正文宽度均分；余数 twip 分给靠前的未指定列。指定了的宽度按原值使用，表宽是各列之和。未指定列分到的宽度小于或等于 0 时，返回 `invalid_tables`，`message` 为 `column widths exceed the text width`。旅行社合同模板的正文宽度是 8306。报价列宽 `1100 + 1600 + 3206 + 700 + 850 + 850`，行程列宽 `1400 + 1200 + 5706`，两组都等于 8306。

#### 单元格

值可以是字符串、整数、浮点数或 null。整数 `0` 写成 `0`。浮点数去掉多余的尾随 0，`3000.0` 写成 `3000`，`0.03` 仍是 `0.03`。null、缺键、空白字符串写成 `empty_cell`。布尔值、数组、对象返回 `invalid_tables`。单元格字符串里的 `\n` 写成 Word 换行。

#### JSON 示例

```json
{
  "template": "旅行社合同模板.docx",
  "placeholders": {"甲方名称": "示例公司", "甲方税号": ""},
  "required": ["甲方名称"],
  "empty_optional": "/",
  "tables": [
    {
      "anchor": "报价",
      "columns": [
        {"key": "分类", "header": "分类", "width": 1100},
        {"key": "名称", "header": "名称", "width": 1600},
        {"key": "描述", "header": "描述", "width": 3206},
        {"key": "数量", "header": "数量", "width": 700, "align": "right"},
        {"key": "单价", "header": "单价", "width": 850, "align": "right"},
        {"key": "总价", "header": "总价", "width": 850, "align": "right"}
      ],
      "rows": [
        {"分类": "交通", "名称": "大巴车（38座）", "描述": "杭州-千岛湖往返", "数量": 1, "单价": 3000, "总价": 3000}
      ]
    },
    {
      "anchor": "行程",
      "header": true,
      "borders": true,
      "empty_cell": "/",
      "columns": [
        {"key": "日期", "header": "日期", "width": 1400},
        {"key": "时间", "header": "时间", "width": 1200},
        {"key": "内容", "header": "内容", "width": 5706}
      ],
      "rows": [
        {"日期": "day1", "时间": "08:00", "内容": "杭州集合出发，前往千岛湖"}
      ]
    }
  ]
}
```

报价表的 `rows` 对应费用明细，行程表的 `rows` 对应活动行程。`甲方税号` 传了空字符串且不在 `required` 里，文档里写成 `/`。

```bash
curl -X POST http://localhost:8060/api/generate \
  -H 'Content-Type: application/json; charset=utf-8' \
  -o contract.docx \
  -d @request.json
```

#### multipart

`Content-Type` 使用 `multipart/form-data`，由客户端生成 boundary。模板字节来自上传的 `file`，服务不读取模板目录，也不使用表单里的 `template`。上传内容不落盘。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | 文件 | 是 | 上传的 `.docx`。没有这个文件时返回 `invalid_template`。下载文件名用上传文件名，没有文件名时用 `filled.docx`。 |
| `placeholders` | 字符串 | 是 | JSON 对象，规则与 JSON 请求相同。缺失或不是 JSON 时返回 `invalid_placeholders`。 |
| `required` | 字符串 | 否 | JSON 数组。省略或空字符串时按 `[]`。 |
| `empty_optional` | 字符串 | 否 | 普通文本，默认空字符串。 |
| `tables` | 字符串 | 否 | JSON 数组，规则与 JSON 请求相同。不是合法 JSON 时返回 `invalid_tables`，`message` 为 `tables is not json`。 |

```bash
curl -X POST http://localhost:8060/api/generate \
  -F 'file=@旅行社合同模板.docx' \
  -F 'placeholders={"甲方名称":"示例公司","甲方税号":""}' \
  -F 'required=["甲方名称"]' \
  -F 'empty_optional=/' \
  -o contract.docx
```

### 成功响应

状态码 200。响应体是 docx 字节，服务不保存生成结果。

- `Content-Type`：`application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- `Content-Disposition`：`attachment; filename="..."; filename*=UTF-8''...`。JSON 请求的文件名是模板文件名，不含目录。
- `X-Docx-Unresolved`：仅在仍有未传入的 `{{key}}` 时出现。

### 常见报错

生成失败时状态码是 400，`Content-Type` 为 `application/json`，响应体不是 docx。下面是服务实际返回的 JSON。`invalid_placeholders` 的 `message` 只取第一条校验结果，不带字段路径。

| 响应 JSON | 什么时候 |
| --- | --- |
| `{"error":"unsupported_media_type"}` | `Content-Type` 不是 `application/json` 或 `multipart/form-data`，例如纯文本、`application/x-www-form-urlencoded` |
| `{"error":"invalid_placeholders","message":"request body is not json"}` | 声明了 JSON，正文却解析失败 |
| `{"error":"invalid_placeholders","message":"request body must be an object"}` | JSON 是数组或其他非对象 |
| `{"error":"invalid_placeholders","message":"Field required"}` | 缺少 `template` 或 `placeholders` |
| `{"error":"invalid_placeholders","message":"Input should be a valid string"}` | `template`、`empty_optional`，或 `placeholders` 里的值不是字符串。数字也走这里 |
| `{"error":"invalid_placeholders","message":"Input should be a valid dictionary"}` | `placeholders` 不是对象，例如 `null` |
| `{"error":"invalid_placeholders","message":"Input should be a valid list"}` | `required` 不是字符串数组 |
| `{"error":"invalid_placeholders","message":"placeholders is required"}` | multipart 里没有 `placeholders` 字段 |
| `{"error":"invalid_placeholders","message":"multipart body is invalid"}` | multipart 正文无法解析 |
| `{"error":"invalid_placeholders","message":"invalid"}` | multipart 的 `placeholders` 或 `required` 不是合法 JSON |
| `{"error":"template_path_invalid","template":"../a.docx"}` | 去掉空白后为空、是绝对路径、路径含 `..`，或后缀不是 `.docx`。`template` 回显处理后的字符串，空白时是 `""` |
| `{"error":"template_not_found","template":"没有.docx"}` | 模板目录里没有这个相对路径 |
| `{"error":"invalid_template"}` | 文件不是合法 docx，或 multipart 没有 `file`。内部原因不出现在响应里 |
| `{"error":"missing_required","missing":["订单号","甲方名称"]}` | 必填键缺失，或去掉空白后为空。`missing` 按请求里的先后顺序，重复键只保留一次 |
| `{"error":"table_anchor_not_found","anchor":"没有这行"}` | 正文里没有 `{{anchor}}`。`anchor` 是去掉空白后的名字 |
| `{"error":"table_anchor_duplicate","anchor":"报价"}` | 同一次请求里锚点重复，或正文里同一个锚点出现多次 |

`invalid_tables` 的外形都是 `{"error":"invalid_tables","message":"..."}`。`message` 是下面这些原文：

| `message` | 什么时候 |
| --- | --- |
| `tables must be a list` | `tables` 不是数组 |
| `tables is not json` | multipart 的 `tables` 不是合法 JSON |
| `table must be an object` | 数组里的一项不是对象 |
| `anchor is invalid` | 锚点为空、不是字符串，或含有 `{`、`}` |
| `columns is required` | 没有 `columns`，或是空数组 |
| `rows must be a list` | `rows` 不是数组 |
| `table row must be an object` | 某一行不是对象，或行的键不是字符串 |
| `header and borders must be booleans` | `header` 或 `borders` 不是布尔值 |
| `empty_cell must be a string` | `empty_cell` 传了非字符串。省略或 `null` 会改用 `empty_optional` |
| `column must be an object` | 某一列不是对象 |
| `column key is required` | `key` 缺失或去掉空白后为空 |
| `column header must be a string` | `header` 不是字符串 |
| `column width must be a positive integer` | 宽度是 0、负数、小数或布尔值。JSON 的 `1.0` 也是小数 |
| `column align must be left, center, or right` | `align` 不是这三个值 |
| `table cell must be a string, number, or null` | 单元格是布尔值、数组、对象，或不是有限数字 |
| `column widths exceed the text width` | 有的列没写 `width`，已经写明的宽度占满或超过正文宽度 |

本地单测：

```bash
cd docx-generation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m unittest test_health.py test_fill.py test_generate.py test_tables.py
```
