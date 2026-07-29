# n8n 飞书自动化

基于 Docker Compose 的 n8n 自动化栈，围绕飞书多维表格、发票 PDF 识别、合同/出团计划单生成、对话机器人等流程运行。

## 服务一览

| 服务 | 作用 | 端口 |
| --- | --- | --- |
| `n8n` | 工作流编排、凭据、Webhook、定时任务 | 5678 |
| `pdf-parser` | PDF 发票文本与字段解析 | 8000 |
| `feishu-listener` | 飞书事件长连接，转发 Webhook | 8010 |
| `feishu-field-parser` | 飞书多维表格字段解析 | 8020 |
| `feishu-files-generation` | 合同 / 出团计划单等文档生成 | 8030 |
| `feishu-bot` | 飞书对话机器人（Skill 平台） | 8040 |

入口配置见 [`compose.yaml`](compose.yaml)。

## 目录说明

```text
compose.yaml          # Docker Compose 栈
.env.example          # 环境变量模板（复制为 .env）
pdf-parser/           # PDF 解析服务
feishu-listener/      # 飞书事件监听
feishu-field-parser/  # 字段解析
feishu-files-generation/
feishu-bot/           # 对话机器人
scripts/              # 辅助脚本与单测
```

本地运行数据（**不入库**）：

| 路径 | 说明 |
| --- | --- |
| `.env` | 密钥与环境变量 |
| `n8n-data/` | n8n 数据库、凭据、日志 |
| `files/` | n8n 共享挂载目录（runtime） |
| `*/data/` | 各服务本地 SQLite 等 |

## 本地启动

```bash
cp .env.example .env
# 编辑 .env，至少填入 N8N_ENCRYPTION_KEY 与飞书相关变量

docker compose up -d --build
```

- n8n 界面：http://localhost:5678
- 常用健康检查：`http://localhost:8000/health`、`http://localhost:8010/health` 等（见各服务 README）

停止：

```bash
docker compose down
```

## 配置要点

- 环境变量以 [`.env.example`](.env.example) 为准；`.env` 勿提交。
- 飞书监听路由见 [`feishu-listener/config.yaml`](feishu-listener/config.yaml)。
- 各服务细节见对应目录下的 `README.md`（`pdf-parser/`、`feishu-listener/`、`feishu-bot/` 等）。
