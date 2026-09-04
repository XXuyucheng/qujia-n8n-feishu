# Feishu Listener

飞书事件长连接监听服务：接收飞书推送、按 `config.yaml` 路由规则筛选，并将匹配事件转发到 n8n Webhook。事件与转发记录写入 SQLite，可通过 Web UI 查看。

## 双应用长连接

| 应用 | 环境变量 | 事件 | 进程 |
| --- | --- | --- | --- |
| 主应用（n8n） | `FEISHU_APP_ID` / `SECRET` | bitable 变更；`im.message.receive_v1`（群内 n8n 机器人，如发票税务校验） | 主进程线程内 `lark.ws.Client` |
| 对话应用（feishu-bot） | `FEISHU_CHAT_APP_ID` / `SECRET` | `im.message.receive_v1` | 子进程 `chat_ws.py` → `POST /internal/ingest` |

同一进程无法跑两个 lark WS Client（asyncio 冲突），故对话应用使用子进程。健康检查字段：`ws_status`、`chat_ws_status`、`chat_ws_pid`。

将对话 IM 切到 `feishu-cardbot` 长连接前：在 `.env` 设 `FEISHU_CHAT_WS_ENABLED=false` 后 **由你择时重启** listener，否则两条长连接会互踢。兼容旧变量 `LISTENER_DISABLE_CHAT_WS=true`。

对话 IM 路由示例见 `config.yaml` 中 `feishu-bot-im-message`（转发至 `http://feishu-bot:8040/api/message`）。供应商表 → 知识库同步走独立路由 `kb-sync-bitable`（默认停用，目标为 n8n webhook，**不要**接到对话 `/api/message`）。

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
3. 单条路由依次检查：表级条件 → `actions`（若配置）→ `field_conditions`（若配置）。
4. 命中且 `dispatch_enabled: true` 时，POST 到 `n8n_webhook_url`；否则只记录日志。

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
| `text_contains` | 消息正文（`resource.text`）须包含该子串；用于 IM 口令触发 |
| `actions` | 按 `action_list[].action` 过滤新建/编辑/删除（见下文） |

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

### 操作类型条件（actions）

在表级条件之上，可增加 `actions`，按飞书 `raw.event.action_list[].action` 区分新建行、编辑行、删除行。

| 配置值 | 含义 |
| --- | --- |
| `record_added` | 新建行 |
| `record_edited` | 编辑行 |
| `record_deleted` | 删除行 |

- **不写 `actions`**：不限制操作类型（与升级前行为一致）。
- **配置格式**：支持字符串或列表，例如 `actions: record_added` 或 `actions: [record_added, record_edited]`。
- **列表内 OR**：`actions: [record_added, record_edited]` 表示新建或编辑均可命中。
- **事件侧 OR**：`action_list` 中任意一条 action 满足即可。
- **别名**：`added` / `new`、`edited` / `update`、`deleted` / `delete` 会归一化为上述 canonical 值。

**示例：仅新建行**

```yaml
  - name: quote-table-new-row-only
    enabled: true
    dispatch_enabled: false
    event_type: drive.file.bitable_record_changed_v1
    app_token: YOUR_APP_TOKEN
    table_id: tbl91gZyDPCLhlva
    actions: record_added
    n8n_webhook_url: http://n8n:5678/webhook/feishu/quote-new-row
    max_attempts: 3
    timeout_seconds: 10
```

**示例：仅编辑行**

```yaml
    actions: record_edited
```

**示例：仅删除行**

```yaml
    actions: record_deleted
```

### 字段级条件（细筛）

在表级与 `actions` 条件之上，可增加 `field_conditions` 列表。**列表内多条条件为 AND（全部满足才命中）。**

服务从通过 `actions` 筛选后的 `action_list` 项中读取指定 `field_id` 的值：新建/编辑优先看 `after_value`；删除行若 `after_value` 无该字段，会 fallback 查 `before_value`。

| 条件写法 | 含义 |
| --- | --- |
| `field_id: fldXXX` + `equals: optYYY` | 该字段变更后的值里，存在 token 等于 `optYYY` |
| `equals: [a, b]` | 等于其中任意一个 |
| `contains: "关键词"` | 变更后的值里，任意 token 包含该子串 |
| `exists: true` | 本次变更的 `after_value` 中出现该字段 |
| `exists: false` | 本次变更的 `after_value` 中未出现该字段 |

**`exists` 与 `equals` 的区别**

- `exists: true`：只关心字段**有没有出现在本次变更**里，不关心具体值。例如字段被改成任意选项都会命中。
- `equals: optXXX`：字段必须出现，且变更后的值里**包含该选项 id** 才命中。
- 不要用 `exists: optXXX` 代替 `equals`——非空字符串在代码里会被当作 `true`，效果等同于 `exists: true`，不会校验选项值。

**示例：多个选项任意一个命中时转发**

字段变成选项 A、B、C 中任意一个都要触发，用 `equals` 列表（OR）：

```yaml
    field_conditions:
      - field_id: fldGQVeaKb
        equals:
          - optDK1JeDR
          - optAnotherOne
          - optThirdOne
```

同一字段的多选项应写在一个 `equals` 列表里；若拆成多条 `field_conditions`，会变成 AND，无法表达「A 或 B 或 C」。

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

**示例：编辑行 + 字段条件（与上例等价，但显式限定为编辑）**

```yaml
  - name: quote-table-edit-with-field
    enabled: true
    dispatch_enabled: true
    event_type: drive.file.bitable_record_changed_v1
    app_token: YOUR_APP_TOKEN
    table_id: tblYYYYYYYY
    actions: record_edited
    field_conditions:
      - field_id: fldGQVeaKb
        equals: optDK1JeDR
    n8n_webhook_url: http://n8n:5678/webhook/feishu/quote-test
    max_attempts: 3
    timeout_seconds: 10
```

### 重要说明（字段匹配语义）

- **精确到「哪个字段出现在本次变更里、变更后的值是什么」**，不是「从旧值 A 变成新值 B 才触发」。
- 新建/编辑默认只检查 `after_value`；删除行会额外检查 `before_value`。
- 配置了 `actions` 时，`field_conditions` 只在对应 action 项上评估（避免编辑事件误命中「新建行 + 字段」规则）。
- 一次编辑可能同时带来多个字段出现在 `after_value`；只要配置的 `field_id` 的变更后值满足条件，就会命中。
- 选项类字段的 `equals` 通常填**选项 id**（如 `optDK1JeDR`），不是显示名称；可在 Web UI 的事件详情 **Field Changes** 中查看。
- 若多条路由可能匹配同一事件，**把更具体的规则（带 `actions`、`field_conditions` 或更小范围 `table_id`）写在前面**。

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

其中 `event` 为标准化后的对象（含 `resource`、`raw` 等）。转发 payload 不会裁剪 `action_list`；更复杂的逻辑仍可在 n8n 工作流内对 `event.raw.event.action_list` 再判断。

### 其它 config 段

```yaml
retention:
  max_days: 7
  max_events: 5000
  vacuum_after_cleanup: false  # 默认关闭；VACUUM 会重写整库，勿在高峰/频繁 recreate 时开启

storage:
  ignored_raw_mode: summary   # 未匹配事件只存摘要，省空间
  matched_raw_mode: full      # 命中事件存完整 raw
```

事件库使用 SQLite WAL。`POST /admin/cleanup` 与 `/admin/compact-ignored` 仅在配置了 `vacuum_after_cleanup: true` 时才会 `VACUUM`；需要压缩体积时请在低峰、写入较少时手动开启并执行。

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
| `POST` | `/admin/cleanup` | 手动清理旧事件（默认不 VACUUM） |
| `POST` | `/admin/compact-ignored` | 压缩 ignored 事件的 raw（默认不 VACUUM） |
| `POST` | `/debug/normalize` | 调试标准化（生产建议 `ENABLE_DEBUG_ENDPOINTS=false`） |

事件数据库路径：`feishu-listener/data/events.sqlite`。

## 测试

```bash
cd feishu-listener
python -m unittest test_routes.py
```
