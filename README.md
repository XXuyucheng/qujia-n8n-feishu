# n8n 自动化项目说明

## 1. 项目概览

当前项目是一个基于 Docker Compose 的 n8n 自动化栈，主要围绕飞书多维表格、发票 PDF 识别、报价生成和应收提醒等流程运行。

项目根目录：

```text
/Users/xuyucheng/My_project/n8n
```

核心服务：

| 服务 | 作用 | 当前技术方式 | 当前端口 |
| --- | --- | --- | --- |
| `n8n` | 工作流编排、凭据管理、Webhook 接收、定时任务执行 | 官方 Docker 镜像 `docker.n8n.io/n8nio/n8n:2.11.3` | `5678:5678` |
| `pdf-parser` | PDF 发票文本与字段解析服务 | Python 3.12 + FastAPI + PyMuPDF | `8000:8000` |
| `feishu-listener` | 飞书事件长连接（主应用 bitable + 对话应用 IM）、转发 Webhook | Python 3.12 + FastAPI + lark-oapi + SQLite | `8010:8010` |
| `supplier-bot` | 供应商录入对话机器人（对话专用应用） | Python 3.12 + FastAPI | `8040:8040` |

当前 Docker Compose 运行状态：

| 容器 | 状态 | 暴露端口 |
| --- | --- | --- |
| `n8n` | Up 4 days | `0.0.0.0:5678->5678/tcp` |
| `pdf-parser` | Up 4 days | `0.0.0.0:8000->8000/tcp` |
| `feishu-listener` | Up 4 days | `0.0.0.0:8010->8010/tcp` |

上云建议：公网只暴露 `80/443`，由反向代理转发到 `n8n:5678`；`pdf-parser` 和 `feishu-listener` 不建议直接暴露到公网。

## 2. 目录结构与持久化数据

当前仓库跟踪的主要文件：

```text
compose.yaml
.env.example
pdf-parser/
feishu-listener/
files/quote-generation-test.workflow.json
files/quote-raw-event-test.json
files/quote-test-payload.json
```

当前本地运行数据：

| 路径 | 当前大小 | 是否纳入 Git | 迁移要求 |
| --- | ---: | --- | --- |
| `.env` | 未统计 | 否 | 必须迁移或在云端重新创建，不能提交到 Git |
| `n8n-data/` | 约 37M | 否 | 必须完整迁移，包含 n8n 数据库、凭据加密配置、日志等 |
| `files/` | 约 32K | 部分文件跟踪，`files/tmp/` 忽略 | 建议完整迁移，排除临时测试文件可选 |
| `feishu-listener/data/` | 约 111M | 否 | 建议迁移，包含飞书事件日志 SQLite |

`n8n-data/` 中的关键文件：

```text
n8n-data/database.sqlite
n8n-data/database.sqlite-wal
n8n-data/database.sqlite-shm
n8n-data/config
n8n-data/nodes/package.json
n8n-data/n8nEventLog*.log
```

迁移 n8n 数据库时，必须在停止本地 n8n 后再复制 `database.sqlite`、`database.sqlite-wal`、`database.sqlite-shm`，否则可能复制到不一致的 SQLite 状态。

## 3. 配置方式

### 3.1 Docker Compose 配置

当前入口文件为：

```text
compose.yaml
```

当前 compose 特点：

- 使用一个 compose 栈启动 `n8n`、`pdf-parser`、`feishu-listener`。
- n8n 通过本地目录 `./n8n-data:/home/node/.n8n` 持久化。
- n8n 通过本地目录 `./files:/files` 挂载共享文件目录。
- 飞书监听器通过 `./feishu-listener/config.yaml:/app/config.yaml:ro` 读取路由配置。
- 飞书监听器通过 `./feishu-listener/data:/data` 保存事件日志数据库。

当前 n8n 访问配置仍是本地开发形态：

```yaml
N8N_HOST=localhost
N8N_PORT=5678
N8N_PROTOCOL=http
N8N_SECURE_COOKIE=false
```

迁移到 ECS + 域名 HTTPS 后，建议调整为：

```env
N8N_HOST=n8n.example.com
N8N_PORT=5678
N8N_PROTOCOL=https
WEBHOOK_URL=https://n8n.example.com/
N8N_SECURE_COOKIE=true
N8N_PROXY_HOPS=1
```

其中 `n8n.example.com` 替换为实际已备案域名。

### 3.2 环境变量配置

当前 `.env.example` 中定义了以下变量：

```env
N8N_ENCRYPTION_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=
FEISHU_BASE_ID=
FEISHU_TABLE_ID=
FEISHU_CHAT_ID=
```

当前本机 `.env` 配置状态：

| 变量 | 当前状态 | 用途 |
| --- | --- | --- |
| `N8N_ENCRYPTION_KEY` | 已配置 | n8n 凭据加密主密钥，迁移时必须保持不变 |
| `FEISHU_APP_ID` | 已配置 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 已配置 | 飞书应用密钥 |
| `FEISHU_VERIFICATION_TOKEN` | 已配置 | 飞书事件订阅校验 Token |
| `FEISHU_ENCRYPT_KEY` | 已配置 | 飞书事件加密 Key |
| `FEISHU_BASE_ID` | 已配置 | 飞书多维表格 Base ID |
| `FEISHU_TABLE_ID` | 已配置 | 飞书表格 ID |
| `FEISHU_CHAT_ID` | 已配置 | 飞书群聊或消息目标 ID |

重要约束：

- `N8N_ENCRYPTION_KEY` 不能重新生成；否则迁移后的 n8n 无法解密旧凭据。
- `.env` 不能提交 Git，只能通过安全方式同步到 ECS。
- 如果使用云端 CI/CD，建议改用服务器本地 `.env`、Docker secret 或受控密钥管理，不要把密钥写入仓库。

### 3.3 飞书监听路由配置

配置文件：

```text
feishu-listener/config.yaml
```

当前路由概况：

| 路由 | 状态 | 事件类型 | 目标 n8n Webhook |
| --- | --- | --- | --- |
| `invoice-bitable-record-changed` | 启用，允许转发 | `drive.file.bitable_record_changed_v1` | `http://n8n:5678/webhook/feishu/invoice` |
| `quote-generation-test-bitable-record-changed` | 启用，允许转发 | `drive.file.bitable_record_changed_v1` | `http://n8n:5678/webhook/feishu/quote-test` |

当前监听器使用飞书长连接 SDK，原则上不需要公网飞书回调 URL。它主动连接飞书，收到事件后按配置筛选，再通过 Docker 内部网络调用 n8n Webhook。

保留配置建议：

- `n8n_webhook_url` 继续使用 `http://n8n:5678/...`，这是容器内部访问地址，不需要改成公网域名。
- 云端部署时不应把 `feishu-listener:8010` 暴露给公网。
- 生产环境建议关闭调试接口：`ENABLE_DEBUG_ENDPOINTS=false`。

## 4. 技术方式

### 4.1 n8n

当前 n8n 使用默认 SQLite 数据库，数据库位于：

```text
n8n-data/database.sqlite
```

当前 n8n 数据概况：

| 项目 | 数量 |
| --- | ---: |
| 工作流 | 9 |
| 凭据 | 1 |

当前工作流状态：

| 工作流 | 状态 |
| --- | --- |
| `发票识别` | 激活 |
| `报价生成-测试` | 激活 |
| `收入关联提醒` | 激活 |
| `My workflow` | 未激活 |
| `发票PDF识别开发测试` | 未激活 |
| `发票识别` | 未激活副本 |
| `应收提醒` | 未激活 |
| `报价输入-文件` | 未激活 |
| `报价输入-文件` | 未激活副本 |

当前 n8n 依赖的外部能力：

- 飞书开放平台 API。
- DeepSeek n8n 社区节点或 LangChain 节点凭据。
- 本地 `pdf-parser` 服务。
- 飞书监听器转发过来的 Webhook 请求。

迁移建议：

- 当前并发预估为 5-10 条工作流，短期可以保留 SQLite。
- 建议在 ECS 上增加 `DB_SQLITE_POOL_SIZE=5`，让 SQLite 使用 WAL 读连接池。
- 如果后续团队规模扩大、执行量明显增加、或需要高可用，再评估迁移到 PostgreSQL 或阿里云 RDS PostgreSQL。

### 4.2 pdf-parser

目录：

```text
pdf-parser/
```

技术栈：

```text
Python 3.12
FastAPI 0.115.6
uvicorn 0.34.0
python-multipart 0.0.20
PyMuPDF 1.25.1
```

接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/parse` | 上传 PDF 并解析文本、发票字段 |

上云建议：

- 不对公网开放 `8000`。
- n8n 内部调用地址使用 `http://pdf-parser:8000/parse`。
- 如果未来要对外提供 PDF 解析 API，需要额外增加鉴权、限流和文件大小限制。

### 4.3 feishu-listener

目录：

```text
feishu-listener/
```

技术栈：

```text
Python 3.12
lark-oapi 1.6.6
FastAPI 0.115.6
uvicorn 0.34.0
httpx 0.28.1
PyYAML 6.0.2
SQLite
```

主要能力：

- 使用飞书长连接监听事件。
- 标准化飞书事件结构。
- 按 `config.yaml` 中的路由规则筛选事件。
- 记录事件和转发尝试到 `feishu-listener/data/events.sqlite`。
- 将匹配事件转发到 n8n Webhook。

接口：

| 方法 | 路径 | 作用 | 云端建议 |
| --- | --- | --- | --- |
| `GET` | `/health` | 健康检查 | 仅内网 |
| `GET` | `/routes` | 查看路由 | 仅内网 |
| `GET` | `/events` | 查看事件列表 | 仅内网 |
| `GET` | `/events/{id}` | 查看事件详情 | 仅内网 |
| `GET` | `/stats` | 查看统计 | 仅内网 |
| `POST` | `/admin/cleanup` | 清理旧事件 | 仅内网 |
| `POST` | `/admin/compact-ignored` | 压缩忽略事件原始数据 | 仅内网 |
| `POST` | `/debug/normalize` | 调试事件标准化 | 生产关闭 |
| `GET` | `/ui` | 简易事件 UI | 仅内网或关闭公网 |

当前保留策略：

```yaml
retention:
  max_days: 7
  max_events: 5000
  vacuum_after_cleanup: true
```

## 5. 当前配置状态与风险

### 5.1 已完成

- Docker Compose 栈可以启动三个服务。
- `.env` 中必需变量均已配置。
- n8n 当前已有工作流和凭据数据。
- 飞书监听器已有两条启用路由。
- `n8n-data/`、`feishu-listener/data/` 已通过 `.gitignore` 排除，不会误提交运行数据库。

### 5.2 云端迁移前必须处理

1. HTTPS 与域名配置

   当前 n8n 是 `localhost + http + insecure cookie` 配置。云端必须改为域名 HTTPS，否则 Webhook URL、登录 Cookie 和第三方回调都容易出问题。

2. 端口暴露

   当前 `5678`、`8000`、`8010` 都暴露到宿主机。云端不建议这样做，应只开放 `80/443`，由反向代理访问 n8n。

3. 调试接口

   当前 `feishu-listener` 使用 `ENABLE_DEBUG_ENDPOINTS=true`。云端生产环境建议关闭。

4. 工作流内密钥写法

   数据库中检测到部分工作流节点仍直接包含 `app_secret` 字段，且部分未引用 `$env.FEISHU_APP_SECRET`。迁移前建议统一改成环境变量引用，并在飞书后台轮换一次应用密钥。

5. SQLite 复制一致性

   迁移时必须停止本地 n8n，再复制数据库文件；不能在 n8n 正运行时直接同步 SQLite 文件。

## 6. 推荐云端部署方式

### 6.1 ECS 规格建议

按当前 5-10 条并行工作流、团队低中频使用评估：

| 资源 | 推荐 |
| --- | --- |
| CPU / 内存 | `4 vCPU / 8 GB` 更稳；低成本可从 `2 vCPU / 4 GB` 起步 |
| 系统盘 | 至少 `40-80 GB` |
| 数据盘 | 可选；若长期保存执行日志和文件，建议单独数据盘 |
| 操作系统 | Ubuntu LTS、Debian 或 Alibaba Cloud Linux |
| 备份 | ECS 云盘快照 + 项目数据目录归档到 OSS |

### 6.2 安全组建议

| 端口 | 来源 | 说明 |
| --- | --- | --- |
| `22` | 只允许你的固定 IP | SSH 管理 |
| `80` | `0.0.0.0/0` | HTTP，主要用于证书签发和跳转 HTTPS |
| `443` | `0.0.0.0/0` | HTTPS 访问 n8n |
| `5678` | 不开放公网 | 仅 Docker 内部或本机反代访问 |
| `8000` | 不开放公网 | 仅 n8n 内部调用 |
| `8010` | 不开放公网 | 仅内网调试或健康检查 |

### 6.3 推荐反向代理

推荐使用 Caddy，原因是配置简单并能自动申请和续期 HTTPS 证书。

示例 Caddyfile：

```caddyfile
n8n.example.com {
  reverse_proxy n8n:5678
}
```

如果使用 Nginx，也可以，但需要额外处理证书签发、续期、WebSocket 和代理头。

## 7. 迁移步骤建议

### 7.1 迁移前准备

1. 确认域名已备案并解析到 ECS 公网 IP。
2. 确认 ECS 安全组只开放 `22`、`80`、`443`。
3. 在 ECS 安装 Docker 和 Docker Compose 插件。
4. 在 ECS 创建部署目录，例如：

```bash
sudo mkdir -p /opt/n8n
sudo chown -R $USER:$USER /opt/n8n
```

5. 准备云端 `.env`，保留原本的 `N8N_ENCRYPTION_KEY`。

### 7.2 停机同步

建议低峰期执行：

1. 在本地停用或停止当前 n8n 容器。
2. 确认本地不再执行定时任务和飞书事件消费。
3. 同步以下内容到 ECS：

```text
compose.yaml
.env
.env.example
pdf-parser/
feishu-listener/
files/
n8n-data/
feishu-listener/data/
```

推荐用 `rsync` 或压缩包传输。同步 `.env` 时注意不要经过公开仓库或聊天工具。

### 7.3 云端调整

在 ECS 上调整 compose：

- n8n 改为 HTTPS 域名配置。
- 增加 `WEBHOOK_URL`。
- 增加 `N8N_PROXY_HOPS=1`。
- `N8N_SECURE_COOKIE=true`。
- 关闭 `feishu-listener` 调试接口。
- 移除或限制 `5678`、`8000`、`8010` 的公网端口映射。
- 增加反向代理服务。

### 7.4 启动与验证

在 ECS 项目目录执行：

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f n8n
```

验证清单：

- `https://你的域名` 可以打开 n8n。
- n8n 可以登录，凭据可以正常解密。
- 激活工作流状态符合预期。
- n8n Webhook 显示 HTTPS 域名。
- `pdf-parser` 健康检查正常。
- `feishu-listener` 健康检查正常。
- 飞书事件能进入监听器并转发到 n8n。
- 发票识别、报价生成、收入关联提醒至少各跑一次真实或测试样例。

### 7.5 切换与回滚

切换完成后：

- 保持本地 n8n 停止，避免定时任务和飞书事件被双实例重复处理。
- 保留本地完整数据副本至少 7-14 天。
- 如果云端验证失败，可停止云端 compose，恢复本地 compose 继续运行。

## 8. 后续同步策略

### 8.1 代码同步

建议把仓库中非敏感内容通过 Git 同步到云端：

```text
compose.yaml
.env.example
pdf-parser/
feishu-listener/
files/*.workflow.json
files/*test*.json
```

不要通过 Git 同步：

```text
.env
n8n-data/
feishu-listener/data/
files/tmp/
*.sqlite
*.log
```

### 8.2 数据同步

n8n 生产数据不建议双向实时同步。推荐方式：

- 云端作为唯一生产实例。
- 本地只作为开发或备份实例。
- 生产数据通过定期备份同步到 OSS 或本地归档。
- 如需本地调试，使用 n8n 导出工作流或恢复备份副本，避免本地和云端同时连接飞书生产事件。

### 8.3 备份建议

每日备份：

```text
.env
n8n-data/
files/
feishu-listener/config.yaml
feishu-listener/data/events.sqlite
```

备份前建议短暂停止 n8n，或至少确保 SQLite 文件一致性。更稳妥的方式是使用云盘快照配合目录级归档。

保留周期建议：

| 类型 | 周期 |
| --- | --- |
| 每日目录备份 | 7-14 天 |
| 每周完整快照 | 4-8 周 |
| 重大变更前手动快照 | 至少保留到变更稳定后 |

## 9. 云端上线前待办清单

- [ ] 确认域名解析和备案状态。
- [ ] 确认 ECS 安全组规则。
- [ ] 准备生产版 `.env`。
- [ ] 保留原 `N8N_ENCRYPTION_KEY`。
- [ ] 将 n8n 改为 HTTPS 域名配置。
- [ ] 增加反向代理。
- [ ] 关闭 `feishu-listener` 调试接口。
- [ ] 不暴露 `5678`、`8000`、`8010` 到公网。
- [ ] 清理工作流中硬编码的飞书密钥。
- [ ] 低峰期停机同步 SQLite 数据。
- [ ] 云端验证三个激活工作流。
- [ ] 建立自动备份和快照策略。

## 10. 结论

当前项目适合以 Docker Compose 方式整体迁移到阿里云 ECS。以当前团队规模和并发量，短期保留 SQLite 是可行方案，迁移成本最低，也能最大程度保持现有凭据和工作流连续性。

真正需要重点处理的是云端入口安全和配置生产化：域名 HTTPS、反向代理、端口收敛、关闭调试接口、保留加密密钥、停机复制 SQLite。完成这些后，该项目可以比较平滑地迁移为云端单实例生产环境。
