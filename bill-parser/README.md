# Bill Parser

微信支付**个人账户**账单解析服务：解压加密 ZIP、按表头定位 CSV/XLSX 明细、规则分类。供 n8n 通过 Docker 内网调用后写入飞书多维表。

个人钱包没有官方账单 API。请用微信导出的「用于个人对账」文件，不要用商户平台交易账单。

## 启动

```bash
docker compose build bill-parser
docker compose up -d bill-parser
```

健康检查：

```bash
curl -s http://localhost:8050/health
# {"ok":true,"service":"bill-parser"}
```

n8n 容器内调用地址：`http://bill-parser:8050`

## 导出微信账单

1. 微信 → 我 → 服务 → 钱包 → 账单 → 常见问题 → **下载账单**
2. 选择 **用于个人对账**（不要选「用于证明材料」的 PDF）
3. 选时间范围，填邮箱，等待 ZIP 和解压码（微信消息里的 6 位数字）
4. 把 ZIP（或解压后的 CSV/XLSX）和密码交给本服务 / n8n Webhook

本服务**不会**爆破 ZIP 密码。

## 与现有「收支监控」表的边界

[`feishu-listener/config.yaml`](../feishu-listener/config.yaml) 里的 `Income_Expenditure_Monitoring`（`tbl8jKSb6kHvKlCL`）是另一张表的支出金额变动监控。**不要**把微信原流水写入该表。请新建「微信账单」多维表。

## 技术栈

- Python 3.12 + FastAPI + uvicorn
- `pyzipper`（微信 ZIP 常为 AES）
- `openpyxl`（XLSX）
- YAML 规则分类（[`config.yaml`](config.yaml)）

## API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/parse` | 解析微信个人账单 |

### `POST /parse`

`multipart/form-data`：

| 字段 | 说明 |
| --- | --- |
| `file` | `.zip` / `.csv` / `.xlsx` |
| `password` | ZIP 解压密码；未加密 ZIP 可空 |
| `platform` | 仅 `wechat` |

```bash
curl -s -X POST http://localhost:8050/parse \
  -F "file=@微信支付账单.zip" \
  -F "password=123456" \
  -F "platform=wechat"
```

`收/支` 为 `收入`/`支出` 的进入 `transactions`；`/` 等中性流水进入 `skipped`，不入账。`交易单号` 是幂等键。

分类规则改 [`config.yaml`](config.yaml) 后重启 `bill-parser` 即可。

## 飞书多维表字段

手工建表，字段名需与下表一致（工作流按字段名写入）。把飞书应用加为协作者。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| 交易时间 | 日期时间 | 毫秒时间戳写入 |
| 交易类型 | 文本 | |
| 交易对方 | 文本 | |
| 商品 | 文本 | |
| 收支 | 单选 | 选项：`收入`、`支出` |
| 金额 | 数字 | 货币 |
| 支付方式 | 文本 | |
| 当前状态 | 文本 | |
| 交易单号 | 文本 | 查重主键 |
| 商户单号 | 文本 | |
| 备注 | 文本 | |
| 分类 | 单选 | 选项需覆盖 `config.yaml`：`未分类`、`餐饮`、`交通`、`购物`、`生活缴费`、`转账红包`、`收入` |
| 来源 | 文本 | 固定「微信」 |
| 导入批次 | 文本 | n8n execution id |

`.env`：

```bash
FEISHU_WECHAT_BILL_BASE_ID=  # 可与 FEISHU_BASE_ID 相同
FEISHU_WECHAT_BILL_TABLE_ID=
```

## n8n 工作流

模板：[`n8n/wechat-bill-import.workflow.json`](n8n/wechat-bill-import.workflow.json)，说明见 [`n8n/README.md`](n8n/README.md)。

链路：Webhook 上传 → `bill-parser` 解析 → 按交易单号 search 去重 → `batch_create`（每批最多 500）→ 返回摘要。

```bash
curl -X POST http://localhost:5678/webhook/wechat-bill-import \
  -F "file=@微信支付账单.zip" \
  -F "password=123456"
```

成功时响应类似：

```json
{
  "ok": true,
  "imported": 2,
  "duplicates": 0,
  "skipped": 1,
  "stats": { "income": 100.0, "expense": 25.5, "count": 2, "skipped": 1 }
}
```

## 本地测试

```bash
cd bill-parser
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m unittest tests.test_parser
```
