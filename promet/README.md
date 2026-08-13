# promet · 提示词

本目录存放 n8n / 自动化相关的系统提示词文稿，便于评审与版本管理。

| 文件 | 用途 |
| --- | --- |
| [online-quote-ondemand-system.md](./online-quote-ondemand-system.md) | 在线报价「按需生成」DeepSeek Agent 的 System Message |

## 如何同步到 n8n

1. 在本文件审阅、编辑定稿。
2. 打开工作流「测试-在线报价生成」→ 节点 `DeepSeek Agent_按需报价` → Options → System Message。
3. 粘贴 **「你是「趣加团建」…」起至文末** 的正文（可去掉本仓库里的一级标题与引用说明段）。
4. 或重新执行 `node scripts/upgrade_online_quote_dual_mode.js` 从该文件重新灌入工作流 JSON 后再 Import。

## 相关

- 工作流：[`../workflows/测试-在线报价生成.json`](../workflows/测试-在线报价生成.json)
- Cursor skill（维护说明）：[`.cursor/skills/online-quote-ondemand-prompt/SKILL.md`](../.cursor/skills/online-quote-ondemand-prompt/SKILL.md)
