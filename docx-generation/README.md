# docx-generation

本地 `.docx` 模板占位符生成服务。把同一段落里被 Word 拆开的 `{{key}}` 拼回去再替换，覆盖正文、表格单元格、页眉和页脚。表格行循环还没做。

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

## 生成

`POST /api/generate`

JSON：

```json
{
  "template": "合同.docx",
  "placeholders": {"甲方名称": "示例公司", "甲方税号": ""},
  "required": ["甲方名称"],
  "empty_optional": "/"
}
```

`template` 只能是模板目录里的相对路径。`placeholders` 的值必须是字符串。必填为空返回 400：`{"error":"missing_required","missing":["甲方名称"]}`。字典里有、但值为空、且不是必填的键，换成 `empty_optional`（默认空字符串）。

也可以用 multipart：文件字段 `file`，`placeholders` 和 `required` 是 JSON 字符串，`empty_optional` 是普通字段。上传内容不落盘。

成功时响应体是 docx。`Content-Disposition` 带 `filename*=UTF-8''...`。文档里仍有没传入的 `{{key}}` 时，响应头 `X-Docx-Unresolved` 是这些键的 JSON 数组，按 UTF-8 百分号编码。这些键留在原文里。

路径含 `..`、绝对路径、不是 `.docx`：400，`error` 为 `template_path_invalid`。文件不存在：400，`error` 为 `template_not_found`。

本地单测：

```bash
cd docx-generation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m unittest test_health.py test_fill.py test_generate.py
```
