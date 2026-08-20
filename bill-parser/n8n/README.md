# n8n：微信账单入账

导入 [`wechat-bill-import.workflow.json`](wechat-bill-import.workflow.json) 后激活。不要把本工作流接到 `feishu-bot` 对话进程。

1. n8n 编辑器 → 导入该 JSON → 激活
2. Webhook 路径必须是 `wechat-bill-import`
3. 在 `.env` 填写 `FEISHU_WECHAT_BILL_BASE_ID` 与 `FEISHU_WECHAT_BILL_TABLE_ID`（可与现有 `FEISHU_BASE_ID` 同 base）
4. 飞书多维表字段见 [`../README.md`](../README.md)

调用：

```bash
curl -X POST http://localhost:5678/webhook/wechat-bill-import \
  -F "file=@微信支付账单.zip" \
  -F "password=123456"
```
