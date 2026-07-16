# Supplier Bot（Skill 平台 · Phase 1）

飞书**对话专用应用**驱动的业务录入助手。当前已 **Skill 化**：供应商是第一个 skill，后续加客户/线索等只需新增 YAML。

与 n8n 主应用隔离：只用 `FEISHU_CHAT_APP_*`。

## 架构

```
用户 → 对话应用 IM
         ↓
feishu-listener（chat_ws 子进程）
         ↓ POST /api/message
supplier-bot
  router → skill runtime → Form Engine → 多维表格
```

## 配置目录（改业务先改这里）

```text
config/
  app.yaml                 # 全局：TTL、默认口令、idle 提示、路由策略
  skills/
    supplier.yaml          # 供应商录入技能
  cards/                   # 预留（Phase 4）
  knowledge/               # 预留（Phase 5）
```

修改 YAML 后：

```bash
docker compose restart supplier-bot
```

一般**无需重建镜像**（`config/` 已挂载）。

### `app.yaml` 常用项

| 键 | 作用 |
| --- | --- |
| `session_ttl_minutes` | 会话超时 |
| `commands.*` | 全局默认口令（skill 可覆盖） |
| `idle_help` | 未命中任何 skill 时的提示 |
| `router.strategy` | 目前 `trigger_first` |

### `skills/*.yaml` 常用项

| 键 | 作用 |
| --- | --- |
| `id` / `name` / `enabled` | 技能标识；`enabled: false` 则不加载 |
| `triggers` | 触发词（消息包含即命中） |
| `prompts.*` | 对话文案（`{labels}` `{name}` `{action}` `{record_id}` 可替换） |
| `commands` | cancel / confirm / skip / overwrite |
| `target.base_id` / `table_id` | 写入的多维表格 |
| `fields` | 字段映射：`name` `type` `required` `suggested` `options` |
| `require_any_group` | 组合必填（任一组齐即可） |
| `aliases` | 自然语言标签 → 逻辑键 |
| `dedupe` | 查重字段与冲突策略 `ask_overwrite` |
| `attachment.field_key` | 消息图片写入哪个逻辑字段 |

**新增录入 skill 步骤（Phase 2）：** 复制 `supplier.yaml` → 改 `id/triggers/target/fields` → 把对话应用加为该表协作者 → 重启。

更完整的平台规划见仓库根目录 [`飞书对话机器人平台-长期规划.md`](../飞书对话机器人平台-长期规划.md)。

## 本地 / Docker

```bash
docker compose up -d --build supplier-bot feishu-listener
curl http://localhost:8040/health    # skills: ["supplier"]
curl http://localhost:8040/skills
curl http://localhost:8010/health    # chat_ws_status=running
```

单测：

```bash
docker compose run --rm --no-deps supplier-bot python -m unittest test_supplier_bot.py -v
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
