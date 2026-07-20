# Feishu Bot（Skill 平台）

飞书**对话专用应用**驱动的业务录入助手。当前已 **Skill 化**：供应商是第一个 skill，后续加客户/线索等只需新增 YAML。

与 n8n 主应用隔离：只用 `FEISHU_CHAT_APP_*`。

目录由原 `supplier-bot` 更名为 `feishu-bot`；技能 id 仍为 `supplier`。

## 架构

```
用户 → 对话应用 IM
         ↓
feishu-listener（chat_ws 子进程）
         ↓ POST /api/message
feishu-bot
  router → skill runtime → Form Engine → 多维表格
```

## 配置目录（改业务先改这里）

```text
config/
  app.yaml                 # 全局：TTL、默认口令、idle 提示、路由策略、覆盖白名单
  skills/
    supplier.yaml          # 供应商录入技能
  cards/                   # 预留（Phase 4）
  knowledge/               # 预留（Phase 5）
```

修改 YAML 后：

```bash
docker compose restart feishu-bot
```

一般**无需重建镜像**（`config/` 已挂载）。

### `app.yaml` 常用项

| 键 | 作用 |
| --- | --- |
| `session_ttl_minutes` | 会话超时 |
| `commands.*` | 全局默认口令（skill 可覆盖） |
| `idle_help` | 未命中任何 skill 时的提示 |
| `router.strategy` | 目前 `trigger_first` |
| `permissions.overwrite_open_ids` | 可覆盖已有记录的飞书 `open_id` 白名单；空=无人可覆盖 |

### `skills/*.yaml` 常用项

| 键 | 作用 |
| --- | --- |
| `id` / `name` / `enabled` | 技能标识；`enabled: false` 则不加载 |
| `triggers` | 触发词（消息包含即命中） |
| `prompts.*` | 对话文案（`{labels}` `{name}` `{action}` `{record_id}` `{detail}` `{label}` `{value}` 可替换） |
| `commands` | cancel / confirm / skip / overwrite |
| `target.base_id` / `table_id` | 写入的多维表格 |
| `fields` | 字段映射：`name` `type` `required` `suggested` `options`；可选 `pattern: digits` |
| `rules` | 关键词规则：自动设字段、名称 pattern、提示文案 |
| `require_any_group` | 组合必填（任一组齐即可） |
| `aliases` | 自然语言标签 → 逻辑键 |
| `dedupe` | 多字段查重与冲突策略 `ask_overwrite` / `on_hit_denied: reject` |
| `ai.extract_on_incomplete` | 缺必填或长文时调用 LLM 整理字段 |
| `attachment.field_key` | 消息图片写入哪个逻辑字段 |

**边界行为摘要：** 账号须纯数字；选项不存在会硬提示并停留补全；查重命中时普通人拒绝、白名单可「覆盖」；含「客户退款」等关键词时类型自动设为客户退款，且名称须形如 `客户退款26071001`。

**运维注意：** 飞书多维表「类型」需含选项 **客户退款**；在 `app.yaml` 填入管理员 `open_id`。

**新增录入 skill 步骤（Phase 2）：** 复制 `supplier.yaml` → 改 `id/triggers/target/fields` → 把对话应用加为该表协作者 → 重启。

更完整的平台规划见仓库根目录 [`飞书对话机器人平台-长期规划.md`](../飞书对话机器人平台-长期规划.md)。

## 本地 / Docker

```bash
docker compose up -d --build feishu-bot feishu-listener
curl http://localhost:8040/health    # service: feishu-bot, skills: ["supplier"]
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

## 飞书应用清单

1. 开启机器人；长连接订阅 `im.message.receive_v1`
2. 权限：单聊读、发消息、下图、多维表编辑、media 上传
3. 应用加入对应 Base 为可编辑协作者
4. `.env`：`FEISHU_CHAT_APP_ID` / `FEISHU_CHAT_APP_SECRET`
