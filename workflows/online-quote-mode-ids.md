# 在线报价双模式 · ID 占位说明

工作流与 listener 使用下列占位符，**补齐真实 id 后**再导入生产：

| 用途 | 占位符 | 填写位置 |
| --- | --- | --- |
| 报价生成方式 field_id | `FIELD_QUOTE_MODE_PLACEHOLDER` | n8n「判定生成方式」`MODE_CONFIG.fieldIds`；[`feishu-field-parser/config.yaml`](../feishu-field-parser/config.yaml) |
| 附件生成 option id | `OPT_ATTACHMENT_PLACEHOLDER` | `MODE_CONFIG.attachment.ids`；listener 可选 equals |
| 按需生成 option id | `OPT_ONDEMAND_PLACEHOLDER` | `MODE_CONFIG.ondemand.ids`；listener 可选 equals |

## 推荐配置

- **独立字段**「报价生成方式」：选项「附件生成」「按需生成」
- **触发**仍用「报价生成状态」=`待生成`（`fldGQVeaKb` / `optDK1JeDR`）
- n8n 按方式字段文本/ id 分支；无需为两种方式拆两条 listener 路由

## 改完后

1. 更新工作流节点「判定生成方式」中的 `MODE_CONFIG`（或改脚本后重跑 `node scripts/upgrade_online_quote_dual_mode.js`）
2. 更新 `feishu-field-parser` 与 `feishu-listener` 注释中的真实 id
3. 重新 Import / 重启 listener
