# Feishu Field Parser

飞书多维表格字段内容解析工具：把飞书事件里的 `field_id` / `field_value` 数组，结合 `config.yaml` 中的 fields meta，解析为以字段名为 key 的可读对象或文本。

## 启动

```bash
docker compose up -d feishu-field-parser
```

国内构建若出现 `load metadata for docker.io/library/python:3.12-slim` 超时，项目已默认使用镜像站 `docker.1ms.run`；也可在 `.env` 中设置 `FEISHU_FIELD_PARSER_BASE_IMAGE` 覆盖。

本地 UI：

```text
http://localhost:8020/ui
```

健康检查：

```text
http://localhost:8020/health
```

## 配置

配置文件为 `feishu-field-parser/config.yaml`，在 Docker Compose 中挂载到 `/app/config.yaml`。新增表格时，在 `tables` 下添加一个 key：

```yaml
tables:
  income_records:
    name: 业务收入记录
    table_id: tblABGfJ0B1sE5tV
    fields:
      - id: fldw9sa0WQ
        type: 3
        name: 类型
        property:
          options:
            - id: optgUHwD7r
              name: 业务收入-定金
```

解析器主要使用 `id`、`type`、`name`、`property.options` 和 `property.dateFormat`。其它飞书 meta 字段可以原样放入，解析器会忽略不需要的部分。

当前已配置的表格 key：

| table_key | 表名 | table_id |
| --- | --- | --- |
| `income_records` | 业务收入记录 | tblABGfJ0B1sE5tV |
| `quote_records` | 订单记录 | tbl91gZyDPCLhlva |
| `invoice_applications` | 发票申请表 | tbl1qdQwcE3jEjlR |
| `expense_records` | 业务支出表 | tbl8jKSb6kHvKlCL |

新增表格时，可将飞书导出的 fields JSON 保存到 `files/`，再运行：

```bash
cd feishu-field-parser
python scripts/merge_table_fields.py ../files/your-fields.json your_table_key 表名 tblXXXXXXXX
```

## API

列出表格：

```bash
curl http://localhost:8020/api/tables
```

解析字段数组（按 `table_key`）：

```bash
curl -X POST http://localhost:8020/api/parse \
  -H 'Content-Type: application/json' \
  -d '{
    "table_key": "income_records",
    "records": [
      { "field_id": "fldw9sa0WQ", "field_value": "optgUHwD7r" }
    ]
  }'
```

解析字段数组（按 `table_id`，适合 n8n 删除事件直接传 `table_id`）：

```bash
curl -X POST http://localhost:8020/api/parse \
  -H 'Content-Type: application/json' \
  -d '{
    "table_id": "tbl91gZyDPCLhlva",
    "records": [
      { "field_id": "fld6UQO969", "field_value": "{\"users\":[{\"name\":\"阿铭\"}]}" }
    ],
    "format": "text"
  }'
```

请求参数：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `table_key` | 二选一 | 配置中的表格 key，如 `quote_records` |
| `table_id` | 二选一 | 飞书表 ID，如 `tbl91gZyDPCLhlva`；与 `table_key` 同时传时优先 `table_key` |
| `records` | 是 | `before_value` / `after_value` 数组，或 JS/JSON 字符串 |
| `format` | 否 | `object`（默认）或 `text` |

`format=object` 返回示例：

```json
{
  "table_key": "quote_records",
  "table_name": "订单记录",
  "object": {
    "策划师": "阿铭",
    "客户需求": "客户想做一日团建，约30人"
  },
  "warnings": []
}
```

`format=text` 额外返回 `text` 字段，适合直接写入飞书多行文本：

```json
{
  "table_key": "quote_records",
  "table_name": "订单记录",
  "object": { "...": "..." },
  "text": "策划师: 阿铭\n客户需求: 客户想做一日团建，约30人",
  "warnings": []
}
```

`records` 也可以传字符串形式的 JS/JSON 数组，方便从飞书日志或 n8n 里直接复制粘贴。

## 解析说明

- 空字符串解析为 `null`。
- JSON 字符串会自动解析。
- `{ "bus_type": [...], "data": [...] }` 会优先取 `data` 的实际值。
- 文本字段会提取 `[{"type":"text","text":"..."}]` 中的纯文本，不再输出 `{type, text}` 结构。
- 单选/多选字段会根据 `property.options[].id` 转成选项名称。
- 日期字段支持毫秒/秒时间戳、已有日期字符串，并按 `property.dateFormat` 格式化（如 `yyyy/MM/dd` 仅输出日期）。
- 人员字段优先使用 `field_identity_value.users`，输出为姓名；多人用逗号分隔，如 `徐雅琪, 魏航杰`。
- 未在 meta 中找到的字段会放在 `_unknown_fields`；`format=text` 时以 `[未配置字段] fldXXX: ...` 追加在末尾。

## n8n 删除留档工作流建议

删除事件经 `feishu-listener` 转发后，可在 n8n 中按以下方式调用：

1. **Code 节点**：从 `event.raw.event` 提取 `table_id` 与 `action_list[].before_value`。
2. **HTTP Request 节点**：
   - URL: `http://feishu-field-parser:8020/api/parse`
   - Body: `{ "table_id": "...", "records": [...], "format": "text" }`
3. **飞书写回**：将响应中的 `text` 写入备份表的「删除内容」字段。

Code 节点提取示例：

```javascript
const event = $json.body?.event || $json.event || $json;
const raw = event.raw?.event || {};
const action = (raw.action_list || []).find(a => a.action === 'record_deleted') || {};
return [{
  table_id: raw.table_id,
  record_id: action.record_id,
  before_value: action.before_value || [],
}];
```

注意：只有已在 `config.yaml` 中配置 fields meta 的表格，字段名才会被正确解析；其它表删除记录会落入 `_unknown_fields`。

## 测试

```bash
cd feishu-field-parser
python3 -m unittest test_parser.py
```
