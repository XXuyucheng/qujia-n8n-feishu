# Feishu Cardbot（卡片交互机器人 · 供应商录入）

飞书**卡片交互机器人**：在卡片上填供应商信息、点按钮确认后写入多维表。与文字版 [`feishu-bot`](../feishu-bot/) 共用同一飞书应用（`FEISHU_CHAT_APP_*` / `cli_a940966ee8385bd7`），但 **默认不开启长连接**，避免抢现网 `feishu-listener` 的 `chat_ws`。

字段校验、查重、覆盖白名单、写表格式对齐 `feishu-bot` 的 `supplier` skill。

查询（查供应商 / 查价差）仍走 `feishu-bot`，本服务不做。

## 官方事件模型

按[卡片交互机器人文档](https://open.feishu.cn/document/develop-a-card-interactive-bot/introduction)：

| 入口                                                      | 行为                                |
| --------------------------------------------------------- | ----------------------------------- |
| `im.chat.access_event.bot_p2p_chat_entered_v1`          | 发欢迎卡                            |
| `application.bot.menu_v6`（`event_key=add_supplier`） | 发录入表单卡                        |
| `im.message.receive_v1`                                 | 文字 → 表单卡；图片 → 收款码附件  |
| `card.action.trigger`                                   | **3 秒内**回 toast + 更新卡片 |

卡片用 schema 2.0 JSON 由 [`config/skills/supplier.yaml`](config/skills/supplier.yaml) 生成（银行/地域选项与 YAML 同步），不使用 CardKit `template_id`。

## 本地 / Docker

```bash
# 只构建/启动 cardbot，不要用它去重启 feishu-listener
docker compose up -d --build feishu-cardbot
curl http://localhost:8050/health    # service: feishu-cardbot, ws_enabled=false
```

单测：

```bash
docker compose run --rm --no-deps feishu-cardbot python -m unittest test_feishu_cardbot.py -v
```

### HTTP 注入（开发默认开启）

无凭证时只返回卡片 JSON，**不会**调用飞书、**不会**写表（`dry_run` 默认 true）：

```bash
curl -s http://localhost:8050/api/inject/p2p-entered \
  -H 'Content-Type: application/json' \
  -d '{"open_id":"ou_xxx"}'

curl -s http://localhost:8050/api/inject/message \
  -H 'Content-Type: application/json' \
  -d '{"open_id":"ou_xxx","message_id":"om_1","text":"你好"}'

curl -s http://localhost:8050/api/inject/card-action \
  -H 'Content-Type: application/json' \
  -d '{
    "operator":{"open_id":"ou_xxx"},
    "action":{
      "value":{"action":"submit_form"},
      "form_value":{
        "supplier_name":"德清茶歇A",
        "account_name":"张三",
        "account_no":"6222001",
        "bank_name":"工行"
      }
    }
  }'
```

真发卡片：`"send": true`（需要 `FEISHU_CHAT_APP_*`）。真写表：`"dry_run": false`（确认写入动作）。

生产可设 `CARDBOT_ENABLE_INJECT=false`。

## 环境变量

| 变量                                | 默认                      | 说明                                                    |
| ----------------------------------- | ------------------------- | ------------------------------------------------------- |
| `CARDBOT_ENABLE_WS`               | `false`                 | **true 才会**建立飞书长连接。开发期必须保持 false |
| `CARDBOT_PORT`                    | `8050`                  | HTTP 端口                                               |
| `CARDBOT_DB_PATH`                 | `/data/sessions.sqlite` | 会话库                                                  |
| `FEISHU_CARDBOT_CONFIG_DIR`       | `/app/config`           | YAML 目录                                               |
| `FEISHU_CHAT_APP_ID` / `SECRET` | 与 feishu-bot 相同        | 同一应用                                                |
| `CARDBOT_ENABLE_INJECT`           | `true`                  | 开发注入接口                                            |

## 上线切流（必须按顺序，且由你执行重启）

同一应用只能有 **一条** 长连接。切流前现网文字 bot 会停收 IM。

1. 开发者后台确认事件/回调为长连接：`im.message.receive_v1`、`im.chat.access_event.bot_p2p_chat_entered_v1`、`application.bot.menu_v6`、`card.action.trigger`。菜单 key 建议 `add_supplier`。
2. 在 `.env` 设 `FEISHU_CHAT_WS_ENABLED=false`，**由你择时**重启 `feishu-listener`（本仓库不会自动重启它）。
3. 设 `CARDBOT_ENABLE_WS=true`，启动/重启 **仅** `feishu-cardbot`。
4. 单聊进入应收到欢迎卡；点按钮应 3 秒内更新，不能出现飞书错误码 200340。

回切文字 bot：先停 cardbot WS（`CARDBOT_ENABLE_WS=false`），再把 `FEISHU_CHAT_WS_ENABLED` 改回 true 并重启 listener。

## 配置

改 [`config/skills/supplier.yaml`](config/skills/supplier.yaml) 后：

```bash
docker compose restart feishu-cardbot
```

`config/` 已挂载，一般无需重建镜像。覆盖白名单在 [`config/app.yaml`](config/app.yaml) 的 `permissions.overwrite_open_ids`。
