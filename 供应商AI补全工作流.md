# 供应商 AI 补全工作流

用 DeepSeek 批量补全飞书供应商表的「类型 / 地域 / 资质」。视图内**全量**分批处理；回写用 HTTP `batch_update`；`record_id` 只取自原 item。

## 触发

| 项 | 值 |
| --- | --- |
| 方式 | n8n **手动触发** |
| 表 | `tbl4vQ8kft1XXSLv` |
| 视图 | `vewjGvlbFc` |
| Base | `$env.FEISHU_BASE_ID`（默认 `Y2ZgbUGWqa0MB4sLmXxc89T5n0P`） |
| 批大小 | 10 |
| 批间等待 | 1 秒 |

## 数据流

```text
手动触发
  → 获取飞书Token
  → 查询供应商视图首页（search + view_id）
  → 分页展开全部记录（翻完，不过滤）
  → 分批10条
       → 组装AI输入（prompt 不含 record_id，按 index）
       → DeepSeek Agent
       → 准备飞书写入数据（按 index 对齐；record_id 来自 batch item；只填空）
       → 是否需要回写？
            → 是：HTTP POST batch_update
       → 批间等待1秒
       → 下一批
```

对齐发票税务校验模式：Code 产出 `updates: [{ record_id, fields }]`，HTTP 节点 `POST .../records/batch_update`。

## 关键规则

- **只填空**：已有类型/地域/资质不覆盖  
- **类型 / 资质**：白名单  
- **地域**：允许列表外；自定义视为低置信  
- **AI 标记**：低置信或自定义地域 → `AI 待核：…`  
- **record_id**：永远来自「分页展开 / 组装」阶段的 item，忽略 AI 若带了该字段  

## 部署

1. 确认 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_BASE_ID`  
2. DeepSeek 凭证与现有工作流共用  
3. 重新导入 [`files/supplier-ai-fill.workflow.json`](files/supplier-ai-fill.workflow.json)  
4. **激活**工作流（Wait 需要）  
5. 确认表内文本字段「AI 标记」存在  

## 本地单测

```bash
node scripts/test_supplier_ai_fill_logic.js
```

## 验收

- [ ] 视图内记录全量进批（含已填字段行；已填项不被覆盖）  
- [ ] 回写走 HTTP `批量回写飞书`，非 Code 内 HTTP  
- [ ] 写入的 `record_id` 与飞书 item 一致  
- [ ] 低置信 / 自定义地域出现 `AI 待核：…`  
