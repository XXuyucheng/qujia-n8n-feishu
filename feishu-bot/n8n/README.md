# n8n 与 feishu-bot 的分工

录入仍在 `feishu-bot` Form Engine。这里只放 **只读查询 Agent** 和 **知识库同步** 模板，导入 n8n 后使用。不要用这些工作流重做录入状态机，也不要把飞书 IM 长连接并进 n8n。

架构说明见 [`docs/adr/001-feishu-bot-vs-n8n-agent.md`](../../docs/adr/001-feishu-bot-vs-n8n-agent.md)。

## 1. 查询 Agent（只读）

文件：[`query-agent.workflow.json`](query-agent.workflow.json)

1. n8n 编辑器 → 导入该 JSON → 激活工作流
2. Webhook 路径必须是 `feishu-bot-query`（与 compose 默认 `N8N_QUERY_WEBHOOK_URL` 一致）
3. 飞书发「查供应商 某某」或「查价差 …」→ `feishu-listener` → `feishu-bot` → 本 webhook → 回复用户
4. 在 Webhook 与 `FormatReply` 之间插入 **AI Agent**，工具只允许检索（知识库 HTTP 或 bitable search）
5. **禁止** 在本工作流调用多维表 create / update record

知识库未就绪时，占位节点会返回说明文字，录入路径不受影响。

查询权限：上线 Agent 时按 `open_id` 过滤，不要把整库价格说出去。payload 已带 `open_id`。

## 2. 知识库同步（不进对话进程）

文件：[`kb-sync-bitable.workflow.json`](kb-sync-bitable.workflow.json)

- 触发 A：每小时（`EveryHour`）
- 触发 B：`feishu-listener` 路由 `kb-sync-bitable`（供应商表变更 → `http://n8n:5678/webhook/kb-sync-bitable`）。该路由默认 **停用**，启用前先导入并激活本工作流
- **不要** 把表变更接到 `feishu-bot` 的 `/api/message`
- 动作：把供应商 / 价格相关表 upsert 到趣加资源知识库
- 查询 Agent 应打知识库，而不是每次现场依赖飞书字段名

将 `SyncStub` 换成真实节点即可。
