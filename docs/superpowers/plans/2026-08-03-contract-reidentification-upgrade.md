# 合同校验重新识别升级 — Implementation Plan

> **For agentic workers:** 产出为可导入的独立工作流文件，**禁止**写入 `n8n-data/database.sqlite`。

**Goal:** 生成可自行导入的「合同校验重新识别（升级）」工作流，实现 DeepSeek/豆包并行、豆包前 10MB/64MB 校验、分次回写、目的地关键词+DeepSeek 兜底。

**Architecture:** 从现网导出为 base，用 Node 构建脚本改节点/连线，写出 importable JSON；不改活库。

**Tech Stack:** n8n workflow JSON、Node.js 构建脚本、飞书 Bitable HTTP、DeepSeek Agent、豆包 Ark Responses API

## Global Constraints

- 只产出导入文件，不写 SQLite
- 目的地：`fldZ9oTx6C`；未识别不写
- 单图 ≤10MB，请求体 ≤64MB
- 回写 A 只写预付款+目的地；回写 B 只写状态+校验信息

---

### Task 1: 构建脚本 + 导出可导入工作流

**Files:**
- Create: [`scripts/contract_reid_upgrade_workflow.js`](../../scripts/contract_reid_upgrade_workflow.js)
- Create: [`workflows/合同校验重新识别-升级.workflow.json`](../../workflows/合同校验重新识别-升级.workflow.json)
- Create: [`workflows/README.md`](../../workflows/README.md)

**Deliverable:** Import JSON；导入前停用旧工作流（webhook `Bill-Re-identification`）。

**Status:** 已完成。重新生成：`node scripts/contract_reid_upgrade_workflow.js`
