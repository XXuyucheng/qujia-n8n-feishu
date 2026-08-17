---
artifact: adr
version: "1.0"
created: 2026-08-17
status: accepted
---

# ADR-001: 录入留在 feishu-bot，查询与知识库同步走 n8n

## Status

Accepted

**Date:** 2026-08-17
**Deciders:** 产品 / 工程（对话机器人平台）

## Context

已考虑用 n8n AI Agent 工作流整包替代 [`feishu-bot`](../../feishu-bot/) 供应商录入。现网录入已包含多轮确认、空值、选项校验、查重权限、收款码附件和飞书长连接隔离。n8n Agent 擅长选工具与一次性抽取（合同 PDF 已如此），不擅长带硬规则的表单状态机。同时规划「查供应商 / 查价差」以及趣加资源知识库：展示仍在飞书多维表，数据将同步到独立库。

## Decision

我们采用分层，而不是替换：

1. **录入（写）** 继续由 `feishu-bot` Form Engine 负责。LLM 只做字段抽取/纠错，**不**决定是否写表、不替代校验。
2. **查询（读）** 由 `feishu-bot` 识别触发词后转发 n8n Agent webhook；机器人只路由与回复飞书。Agent **不得**创建/更新多维表记录。
3. **同步** 由 n8n 定时或表变更把多维表同步到趣加资源知识库。同步不进入对话进程。
4. **飞书 IM 长连接** 仍由 `feishu-listener` 承担，不并入 n8n。

明确不做：用 n8n 重写 Form Engine；让 Agent 直接 `create record`。

## Consequences

### Positive

- 录入边界（空值、农商归并、退款名称、覆盖 ACL）保持可单测、可回归。
- 查询可用 Agent 工具检索知识库或 bitable，自然语言更松。
- 知识库落地后，价差逻辑不绑死在飞书字段名上。

### Negative

- 查询依赖 n8n webhook 可用；未导入工作流时只能返回配置提示。
- 进行中的录入会话会粘住 skill，需「取消」后再查。

### Neutral

- 知识库未就绪时，Agent 可暂用 bitable search 做 spike，不改变录入架构。

## Alternatives Considered

### 整包迁到 n8n Agent

录入质量提升有限，会话/附件/写表错误（如 `FieldNameNotFound`）要重做且难测。未采用。

### 查询也写在 feishu-bot 内

知识库稳后可改为 bot 直调 API。现阶段复用 n8n 工具与已有 webhook 模式更快。

## References

- [`feishu-bot/README.md`](../../feishu-bot/README.md)
- [`feishu-bot/n8n/README.md`](../../feishu-bot/n8n/README.md)
- n8n docs: Agent vs Basic LLM Chain
