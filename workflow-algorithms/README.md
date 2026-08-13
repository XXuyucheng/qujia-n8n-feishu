# workflow-algorithms

可粘贴进 n8n Code 节点的表格/业务算法源码（与工作流 JSON 同步维护）。

| 文件 | 对应节点 | 说明 |
| --- | --- | --- |
| [quote-attachment-extract.js](./quote-attachment-extract.js) | 「测试-在线报价生成」→ **报价信息提取** | 附件报价：A–G 归一化（键最多行定序）+ 动态表头 + 活动总价边界 |
| [detect-quote-generate-mode.js](./detect-quote-generate-mode.js) | 「测试-在线报价生成」→ **判定生成方式** | 从「报价生成状态」识别附件/按需；仅按需校验人数/出发地目的地/日期/天数 |
| [assemble-online-quote-request.js](./assemble-online-quote-request.js) | 「测试-在线报价生成」→ **组装生成请求** | 拼装 `/api/generate`；附件校人数；按需校订单号/出发地目的地/日期/天数；策划师/目录两种都校 |
| [departure-booking-info.js](./departure-booking-info.js) | 「出团计划单生成-在线报价」→ **预定信息生成** | 按明细名 DAY1/DAY2 相对执行日期推算预定时间；生成供应商预定文案；拼行程正文 |
| [departure-plan-aggregate.js](./departure-plan-aggregate.js) | 「出团计划单生成-在线报价」→ **出团信息聚合** | 组装 fields/sheet_rows；按策划师 open_id 映射 `folder_token`；缺策划师/缺目录时 `ok=false` |

## 同步到工作流

```bash
node scripts/sync_quote_attachment_extract.js
node scripts/sync_detect_quote_generate_mode.js
node scripts/sync_assemble_online_quote_request.js
```

分别写入 [`workflows/测试-在线报价生成.json`](../workflows/测试-在线报价生成.json) 对应 Code 节点。

也可手动：全选算法文件内容 → 粘贴到 n8n Code 节点。

## 输入 / 输出摘要

### quote-attachment-extract

**输入**：`$input` = Extract from File 全部行；订单号来自 `$('读取订单记录')`。

**列顺序**：遍历行数组，取 **`Object.keys` 最多且 ≥6** 的对象（最多取 7 个键 → A–G）。表头行常缺类目列，需用满键明细行定序，否则「活动总价」会对不上。

**活动总价**：任一字格文本含「活动总价」，且同行其余格恰有一个数字（不要求必须在 A 列 / F 列）。

**输出**：`活动名称` / `报价人数` / `活动天数` / `活动日期` / `税费及服务` / `sheet_rows` 等（见算法文件末尾 `return`）。

### detect-quote-generate-mode

**输入**：`$input` =「读取订单记录」。

**识别**：读字段 **「报价生成状态」**；文案含「按需」→ `ondemand`，含「附件」→ `attachment`。

**校验**：仅从「客户需求」解析人数/出发地/目的地/日期/天数；`attachment` 不校验。不读订单表结构化业务字段。

**输出**：`validation_ok` / `error_code` / `generate_mode` / `is_attachment` / `is_ondemand` 等。

### assemble-online-quote-request

**输入**：上游报价 JSON；订单字段来自 `$('读取订单记录')`；Token 来自 `$('获取飞书Token')`。

**校验失败**：`validation_ok=false`，`error_code` 为首个短码。
- **附件**：`缺人数`
- **按需**：`缺订单号` / `缺出发地` / `缺目的地` / `缺出发日期` / `缺活动天数`
- **共用**：`缺策划师` / `缺输出目录`

**校验成功**：带 `folder_token` / `fields` / `sheet_rows` / `itinerary_rows`，可 POST `feishu-files-generation/api/generate`。
### departure-booking-info

**输入**：上游「在线报价信息提取」；执行字段来自 `$('查询待识别执行记录')`。

**预定时间**：从明细 `名称` 解析 `DAY1` / `day2` / `D3` / `第N天`；相对执行日期偏移 `N-1` 天。名称无 day 标记则用执行日期。

**输出**：每条 `报价明细条目` 带 `预定信息`；补齐 `活动行程`；`_meta.exec_record_id` / `order_record_id`。

### departure-plan-aggregate

**输入**：上游「预定信息生成」；执行字段来自 `$('查询待识别执行记录')`。

**输出目录**：执行表「策划」/「策划师」人员字段的 `id`/`open_id` → `PLANNER_OUTPUT_FOLDER` → `folder_token`（与在线报价同一套映射）。

**校验失败**：`ok=false`，`error_message` 为 `缺少策划师` 或 `缺少输出目录`（供 If → 回写「计划单生成状态」）。

**校验成功**：`folder_token` / `fields` / `sheet_rows`，可 POST `feishu-files-generation/api/generate`。

工作流文件：[`workflows/出团计划单生成-在线报价.json`](../workflows/出团计划单生成-在线报价.json)（不写 n8n DB，需自行导入）。
