# Feishu Field Parser

飞书多维表格字段内容解析工具：把飞书事件里的 `field_id` / `field_value` 数组，结合 `config.yaml` 中的 fields meta，解析为以字段名为 key 的 JS/JSON 对象。

## 启动

```bash
docker compose up -d feishu-field-parser
```

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

解析器主要使用 `id`、`type`、`name` 和 `property.options`。其它飞书 meta 字段可以原样放入，解析器会忽略不需要的部分。

## API

列出表格：

```bash
curl http://localhost:8020/api/tables
```

解析字段数组：

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

返回：

```json
{
  "table_key": "income_records",
  "table_name": "业务收入记录",
  "object": {
    "类型": "业务收入-定金"
  },
  "warnings": []
}
```

`records` 也可以传字符串形式的 JS/JSON 数组，方便从飞书日志或 n8n 里直接复制粘贴。

## 解析说明

- 空字符串解析为 `null`。
- JSON 字符串会自动解析。
- `{ "bus_type": [...], "data": [...] }` 会优先取 `data` 的实际值。
- 单选字段会根据 `property.options[].id` 转成选项名称。
- 日期时间戳会按 `Asia/Shanghai` 输出为 `YYYY-MM-DD HH:mm:ss`。
- 人员字段优先使用 `field_identity_value.users`。
- 未在 meta 中找到的字段会放在 `_unknown_fields`，避免丢数据。
