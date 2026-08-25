# Feishu Bot（Skill 平台）

飞书**对话专用应用**驱动的业务录入助手。当前已 **Skill 化**：供应商是第一个 skill，后续加客户/线索等只需新增 YAML。

与 n8n 主应用隔离：只用 `FEISHU_CHAT_APP`。

`feishu-bot`；技能 id 为 `supplier`。

## 架构

录入留在本服务；查询转发 n8n Agent；多维表 → 知识库同步由 n8n 承担（不进对话进程）。详见 [ADR-001](../docs/adr/001-feishu-bot-vs-n8n-agent.md)。

```
用户 → 对话应用 IM
         ↓
feishu-listener（chat_ws 子进程，长连接不并入 n8n）
         ↓ POST /api/message
feishu-bot
  ├─ skill supplier（录入）→ Form Engine → 多维表
  └─ skill query（只读）→ n8n Agent webhook → 回复用户
n8n（定时 / 表变更，非 /api/message）→ 趣加资源知识库
```

n8n 导入模板：[n8n/README.md](n8n/README.md)。`.env` 中 `N8N_QUERY_WEBHOOK_URL` 默认 `http://n8n:5678/webhook/feishu-bot-query`。

进行中的录入会话会粘住 skill：要查询请先发「取消」。

## 配置目录（改业务先改这里）

```text
config/
  app.yaml                 # 全局：TTL、默认口令、idle 提示、路由策略、覆盖白名单
  skills/
    supplier.yaml          # 供应商录入（写表）
    query.yaml             # 只读查询（转发 n8n Agent）
  cards/                   # 预留（Phase 4）
  knowledge/               # 预留（Phase 5）
n8n/
  query-agent.workflow.json
  kb-sync-bitable.workflow.json
```

修改 YAML 后：

```bash
docker compose restart feishu-bot
```

一般**无需重建镜像**（`config/` 已挂载）。

### `app.yaml` 常用项

| 键                                 | 作用                                                  |
| ---------------------------------- | ----------------------------------------------------- |
| `session_ttl_minutes`            | 会话超时                                              |
| `commands.*`                     | 全局默认口令（skill 可覆盖）                          |
| `idle_help`                      | 未命中任何 skill 时的提示                             |
| `router.strategy`                | 目前`trigger_first`                                 |
| `channels.reply_mode`            | 录入默认`card`（互动卡片）；查询仍为文本            |
| `permissions.overwrite_open_ids` | 可覆盖已有记录的飞书`open_id` 白名单；空=无人可覆盖 |

### `skills/*.yaml` 常用项

| 键                                | 作用                                                                                                                   |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `id` / `name` / `enabled`   | 技能标识；`enabled: false` 则不加载                                                                                  |
| `action`                        | 默认`upsert_record`（写表）；`search` 只转发 n8n，不要求 `fields`/`table_id`                                   |
| `triggers`                      | 触发词（消息包含即命中）                                                                                               |
| `prompts.*`                     | 对话文案（`{labels}` `{name}` `{action}` `{record_id}` `{detail}` `{label}` `{value}` 可替换）           |
| `commands`                      | cancel / confirm / skip / overwrite                                                                                    |
| `target.base_id` / `table_id` | 写入的多维表格                                                                                                         |
| `fields`                        | 字段映射：`name` `type` `required` `suggested` `options`；可选 `pattern: digits`、`required_unless_rule` |
| `rules`                         | 关键词规则：自动设字段、名称 pattern、提示文案                                                                         |
| `require_any_group`             | 组合必填（任一组齐即可）                                                                                               |
| `aliases`                       | 自然语言标签 → 逻辑键                                                                                                 |
| `dedupe`                        | 多字段查重与冲突策略`ask_overwrite` / `on_hit_denied: reject`                                                      |
| `ai.extract_enabled`            | 录入时是否调用 LLM 抽字段（默认 true）；校验与写表仍走 Form Engine                                                     |
| `ai.extract_on_incomplete`      | 缺必填或长文时调用 LLM 整理字段                                                                                        |
| `ai.chat_enabled`               | 必须为 false：禁止把录入做成自由 Agent 对话                                                                            |
| `attachment.field_key`          | 消息图片写入哪个逻辑字段                                                                                               |

**边界行为摘要：** 账号须纯数字；选项不存在会硬提示并停留补全；查重仅比对**供应商名称**与**账号**（户名可重名）；命中时普通人拒绝、白名单可「覆盖」；含「客户退款」等关键词时类型自动设为客户退款，且名称须形如 `客户退款26071001`。

**运维注意：** 飞书多维表「类型」需含选项 **客户退款**；在 `app.yaml` 填入管理员 `open_id`。

**新增录入 skill 步骤（Phase 2）：** 复制 `supplier.yaml` → 改 `id/triggers/target/fields` → 把对话应用加为该表协作者 → 重启。

更完整的平台规划见仓库根目录 [`飞书对话机器人平台-长期规划.md`](../飞书对话机器人平台-长期规划.md)。

## 本地 / Docker

```bash
docker compose up -d --build feishu-bot feishu-listener
curl http://localhost:8040/health    # service: feishu-bot, skills 含 supplier / query
curl http://localhost:8040/skills
curl http://localhost:8010/health    # chat_ws_status=running
```

单测：

```bash
docker compose run --rm --no-deps feishu-bot python -m unittest test_feishu_bot.py -v
```

## 对话示例

```
添加供应商：德清某某茶歇
户名：张三
账号：6222001
银行：工行
地域：德清
类型：餐厅
结算类型：月结
联系方式：13800000000
```

确认后写入；补全阶段可发收款码（独立图片或富文本插图）。

查询示例：`查供应商 德清某某茶歇`、`查价差 团餐`。知识库未接入时 n8n 占位工作流会回说明文字，不会写表。

## 飞书应用清单

1. 开启机器人。**事件配置**与**回调配置**分开设，都选 **使用长连接接收**（不要填公网 URL）：
   - 事件：订阅 `im.message.receive_v1`
   - 回调：只订阅新版 **卡片回传交互 `card.action.trigger`**
   - 删除旧版 `card.action.trigger_v1`
   - 机器人能力页若还有 **消息卡片请求网址**，清空后**发布应用版本**
   - 否则飞书会再打一枪 HTTP，客户端仍报 **200671**（即使长连接已回 200）
2. 权限：单聊读、发消息、下图、多维表编辑、media 上传
3. 应用加入对应 Base 为可编辑协作者
4. `.env`：`FEISHU_CHAT_APP_ID` / `FEISHU_CHAT_APP_SECRET`

录入补全/确认使用 JSON 2.0 互动卡片（类型下拉、联系方式输入、确认/取消）。卡片点击由 `feishu-listener` 的 `chat_ws` 同步 `POST http://feishu-bot:8040/api/card-action`。

客户退款识别词：`客户退款`、`退款客户`、`客户退定金`、`客户退订金`、`客户退回`、`退款给客户`（**不含**单独「客户」）。名称须含客户订单号，例如 `客户退款26071001`。非退款供应商必须填写联系方式和类型；类型不在选项内时回复会列出可选值。
