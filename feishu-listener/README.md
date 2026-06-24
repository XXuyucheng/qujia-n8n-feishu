# Feishu Listener

飞书事件长连接监听服务：接收飞书推送、按 `config.yaml` 路由规则筛选，并将匹配事件转发到 n8n Webhook。事件与转发记录写入 SQLite，可通过 Web UI 查看。

## 运行方式

服务使用飞书/Lark Python SDK 长连接客户端，**不需要**公网 HTTP 回调 URL。它主动连接飞书，标准化事件后匹配 `config.yaml` 中的 `routes`，并将日志写入 `feishu-listener/data/events.sqlite`。

修改 `config.yaml` 后**无需重启容器**，下一条事件会自动加载新规则（配置文件在 compose 中挂载为 `/app/config.yaml`）。

## 环境变量

在项目根目录 `.env` 中配置：

```env
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=
```

若飞书事件订阅未启用校验，`FEISHU_VERIFICATION_TOKEN` 和 `FEISHU_ENCRYPT_KEY` 可留空。

## 启动

```bash
docker compose up -d feishu-listener
```

## 路由规则（config.yaml）

**路由规则的唯一配置入口是 `feishu-listener/config.yaml` 中的 `routes` 列表。** UI 仅用于查看事件与当前规则，不能在 UI 里修改路由。

### 匹配流程

1. 飞书推送原始事件 → 服务标准化为 `normalized` 结构。
2. 按 `routes` **从上到下**逐条匹配，**第一条命中即停止**。
3. 命中且 `dispatch_enabled: true` 时，POST 到 `n8n_webhook_url`；否则只记录日志。

### 表级条件（粗筛）

以下字段在路由里**写了就必须与事件完全一致**；**不写表示不限制**：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 是否参与匹配，默认 `true` |
| `dispatch_enabled` | 命中后是否转发 n8n；`false` 时只记日志不调用 Webhook |
| `event_type` | 事件类型，如 `drive.file.bitable_record_changed_v1` |
| `app_token` | 多维表格 Base（与事件中的 `app_token` / `file_token` 比对） |
| `file_token` | 文件 token（可选，一般与 `app_token` 二选一即可） |
| `table_id` | 表 ID |
| `chat_id` | 群聊 ID（消息类事件） |
| `command` | 斜杠命令，如 `/quote` |

**示例：某张表任意字段变动都转发**

```yaml
routes:
  - name: invoice-bitable-record-changed
    enabled: true
    dispatch_enabled: true
    event_type: drive.file.bitable_record_changed_v1
    app_token: YOUR_APP_TOKEN
    table_id: tblXXXXXXXX
    n8n_webhook_url: http://n8n:5678/webhook/feishu/invoice
    max_attempts: 3
    timeout_seconds: 10
```

`n8n_webhook_url` 请继续使用 Docker 内网地址 `http://n8n:5678/...`，上云后也不需要改成公网域名。

### 字段级条件（细筛）

在表级条件之上，可增加 `field_conditions` 列表。**列表内多条条件为 AND（全部满足才命中）。**

服务从飞书事件的 `raw.event.action_list[].after_value` 中读取指定 `field_id` 的**变更后值**进行判断。

| 条件写法 | 含义 |
| --- | --- |
| `field_id: fldXXX` + `equals: optYYY` | 该字段变更后的值里，存在 token 等于 `optYYY` |
| `equals: [a, b]` | 等于其中任意一个 |
| `contains: "关键词"` | 变更后的值里，任意 token 包含该子串 |
| `exists: true` | 本次变更的 `after_value` 中出现该字段 |
| `exists: false` | 本次变更的 `after_value` 中未出现该字段 |

**示例：仅当某单选字段变为指定选项时才转发**

```yaml
  - name: quote-generation-test-bitable-record-changed
    enabled: true
    dispatch_enabled: true
    event_type: drive.file.bitable_record_changed_v1
    app_token: YOUR_APP_TOKEN
    table_id: tblYYYYYYYY
    field_conditions:
      - field_id: fldGQVeaKb
        equals: optDK1JeDR
    n8n_webhook_url: http://n8n:5678/webhook/feishu/quote-test
    max_attempts: 3
    timeout_seconds: 10
```

### 重要说明（字段匹配语义）

- **精确到「哪个字段出现在本次变更里、变更后的值是什么」**，不是「从旧值 A 变成新值 B 才触发」。
- 只检查 `after_value`，**不**比较 `before_value`。
- 一次编辑可能同时带来多个字段出现在 `after_value`；只要配置的 `field_id` 的变更后值满足条件，就会命中。
- 选项类字段的 `equals` 通常填**选项 id**（如 `optDK1JeDR`），不是显示名称；可在 Web UI 的事件详情 **Field Changes** 中查看。
- 若多条路由可能匹配同一事件，**把更具体的规则（带 `field_conditions` 或更小范围 `table_id`）写在前面**。

### 如何查清该写什么

1. 在飞书里做一次希望触发的操作。
2. 打开 Web UI（见下文 SSH 隧道），在 Events 中找到该条记录。
3. 在 Detail → **Field Changes** 中查看 `field_id`、变更后的 **Value**（选项 id 或文本）。
4. 在 Summary 中查看 `app_token`、`table_id`。
5. 编辑 `feishu-listener/config.yaml`，保存即可。

未命中任何路由时，事件状态为 `ignored`，`route_name` 为空。

### 转发到 n8n 的请求体

```json
{
  "route_name": "your-route-name",
  "event": { }
}
```

其中 `event` 为标准化后的对象（含 `resource`、`raw` 等）。更复杂的「从空到有值」等逻辑可在 n8n 工作流内对 `event.raw.event.action_list` 再判断。

### 其它 config 段

```yaml
retention:
  max_days: 7
  max_events: 5000
  vacuum_after_cleanup: true

storage:
  ignored_raw_mode: summary   # 未匹配事件只存摘要，省空间
  matched_raw_mode: full      # 命中事件存完整 raw
```

## Web UI 与 SSH 隧道访问

本地开发：

```text
http://localhost:8010/ui
```

服务部署在 ECS 等远程服务器时，**不要将 8010 对公网开放**。仅自己维护时，用 SSH 本地端口转发即可：

```bash
ssh -L 8010:127.0.0.1:8010 你的用户名@ECS公网IP
```

保持该终端连接，在本机浏览器打开：

```text
http://localhost:8010/ui
```

说明：

- `-L 8010:127.0.0.1:8010` 表示本机 8010 转发到服务器本机 8010（需 compose 仍将 `8010:8010` 映射到宿主机，或改为只绑定 `127.0.0.1:8010:8010`）。
- 若 SSH 使用非 22 端口，加上 `-p 端口`。
- 若本机 8010 已被占用，可改用 `-L 18010:127.0.0.1:8010`，浏览器访问 `http://localhost:18010/ui`。

UI 功能：概览统计、只读路由列表、最近事件（默认最多保留 5000 条）、字段级搜索与事件详情（含 Field Changes、JSON 树）。

## HTTP 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/routes` | 查看当前路由（只读） |
| `GET` | `/events` | 事件列表 |
| `GET` | `/events/{id}` | 事件详情 |
| `GET` | `/stats` | 统计信息 |
| `GET` | `/ui` | Web UI |
| `POST` | `/admin/cleanup` | 手动清理旧事件 |
| `POST` | `/admin/compact-ignored` | 压缩 ignored 事件的 raw |
| `POST` | `/debug/normalize` | 调试标准化（生产建议 `ENABLE_DEBUG_ENDPOINTS=false`） |

事件数据库路径：`feishu-listener/data/events.sqlite`。
