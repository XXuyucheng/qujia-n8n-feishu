# Contract Generator

飞书云文档合同生成服务（路径 B）：复制 docx 模板 → 遍历 Block → 替换 `{{占位符}}` → 返回新文档 URL。

供 n8n / curl 通过 Docker 内网调用。端口默认 `8030`。

上级设计文档：

- [合同生成工作流.md](../合同生成工作流.md)（路径选型）
- [合同生成-路径B实现.md](../合同生成-路径B实现.md)（实现规格）

---

## 调用原理

飞书**没有**模板变量合并 API。本服务按官方推荐流程：

1. `drive/v1/files/{template_token}/copy` — 把模板复制到目标文件夹
2. `docx/v1/documents/{id}/blocks/{id}/children?with_descendants=true` — 拉取整棵块树
3. 在每个含 `text.elements[].text_run` 的块里，用正则替换 `{{key}}`
4. `docx/v1/documents/{id}/blocks/batch_update` — 分批写回（保留 `text_element_style`）

```text
curl / n8n
    │
    ▼
POST /api/generate
    │
    ├─ resolve_template（signing_unit 或显式 token）
    ├─ build_placeholder_values（字段映射 / @today / 金额格式化）
    ├─ 校验必填占位符
    ├─ FeishuClient.copy_file（全局锁 + 限流重试）
    ├─ list_all_blocks
    ├─ block_filler.build_update_requests
    └─ batch_update（每批 ≤20，间隔 ≥350ms）
    │
    ▼
{ document_url, replaced_blocks, unresolved_placeholders }
```

```mermaid
flowchart LR
  req[POST_api_generate] --> resolve[resolve_template]
  resolve --> values[build_placeholder_values]
  values --> copy[drive_files_copy]
  copy --> blocks[list_all_blocks]
  blocks --> fill[replace_in_block]
  fill --> batch[batch_update]
  batch --> resp[document_url]
```

### 两种入参模式

| 模式 | 何时用 | 关键字段 |
| --- | --- | --- |
| **显式 placeholders** | POC / 调试 | `template_token` + `folder_token` + `placeholders` |
| **配置驱动** | 生产（按签约单位选模板） | `signing_unit` + `fields`（field-parser 输出的对象） |

显式模式示例：

```bash
curl -X POST http://localhost:8030/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_access_token": "t-xxx",
    "template_token": "doxcnXXXXXXXX",
    "folder_token": "fldcnXXXXXXXX",
    "document_name": "POC-合同-001",
    "placeholders": {
      "甲方名称": "杭州某某科技有限公司",
      "乙方名称": "杭州趣加旅社有限公司",
      "合同价款": "80,000.00",
      "活动日期": "2026/06/25"
    }
  }'
```

配置驱动示例（`config.yaml` 中已配置「趣加旅社」）：

```bash
curl -X POST http://localhost:8030/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_access_token": "t-xxx",
    "signing_unit": "趣加旅社",
    "fields": {
      "订单号": "TEST-001",
      "单位": "杭州某某科技有限公司",
      "合同价款": 80000,
      "执行日期": "2026/06/25",
      "执行人数": "120",
      "联系人": "张三",
      "联系方式": "13800138000",
      "客户需求": "两日团建",
      "策划师": "阿铭"
    }
  }'
```

`tenant_access_token` 可省略：若容器已配置 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，服务会自行获取。

### 占位符语法与模板规范

- 模板正文写 `{{甲方名称}}`（双花括号，key 无空格）
- `config.yaml` 的 `placeholders` 映射：
  - `{单位}` — 从 `fields` 取值
  - `@today` — 当天 `yyyy年MM月dd日`
  - `@party_b_full_name` — 模板配置的乙方全称
- **强制**：每个 `{{...}}` 必须在**同一个 text_run** 内，不要拆成不同加粗/颜色片段（否则正则匹配不到）
- 用 `POST /api/probe` 扫描模板，核对 key 是否齐全；若出现「跨 text_run」警告，请改模板

### API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/templates` | 列出 config 中的模板 |
| `POST` | `/api/probe` | 扫描模板内 `{{...}}` |
| `POST` | `/api/generate` | 复制 + 填充 |
| `POST` | `/api/generate/preview` | dry-run，只算占位符，不写飞书 |

---

## 项目文件结构

```text
contract-generator/
├── app.py                 # FastAPI 入口与主流程
├── feishu_client.py       # 飞书 API：token / copy / blocks / batch_update
├── block_filler.py        # 块树占位符替换、probe、未解析检测
├── config_loader.py       # config.yaml 加载、模板解析、字段格式化
├── models.py              # Pydantic 请求/响应模型
├── config.yaml            # 签约单位 → 模板 token / 占位符映射
├── requirements.txt
├── Dockerfile
├── README.md
├── test_block_filler.py   # 块替换单元测试（无网络）
├── test_config_loader.py  # 配置与映射单元测试
└── scripts/
    └── smoke_generate.sh  # 真实飞书 POC 冒烟（需 token）
```

职责边界：

| 组件 | 做什么 | 不做什么 |
| --- | --- | --- |
| contract-generator | 复制模板、替换占位符 | 读多维表格、回写状态 |
| n8n | 触发、取数、调本服务、回写 | Block 遍历细节 |
| feishu-field-parser | field_id → 字段名 | 文档生成 |

---

## 启动

```bash
# 在仓库根目录
docker compose build contract-generator
docker compose up -d contract-generator
curl http://localhost:8030/health
# {"status":"ok"}
```

环境变量（见根目录 `.env.example`）：

| 变量 | 说明 |
| --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 未传 token 时自取凭证 |
| `FEISHU_CONTRACT_TEMPLATE_TOKEN` | defaults 兜底模板 token |
| `FEISHU_CONTRACT_OUTPUT_FOLDER` | defaults 兜底输出文件夹 |
| `CONTRACT_GENERATOR_CONFIG_PATH` | 默认 `/app/config.yaml` |

修改 `config.yaml` 后无需重建镜像（已只读挂载），重启容器即可：

```bash
docker compose restart contract-generator
```

---

## 测试步骤

### 1. 单元测试（不依赖飞书）

推荐在 Docker 内用 Python 3.12 跑（本机若是 3.14，pydantic 可能无 wheel）：

```bash
cd contract-generator

docker run --rm -v "$PWD":/app -w /app \
  docker.1ms.run/library/python:3.12-slim \
  bash -c 'pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && python -m unittest discover -v'
```

期望：`test_config_loader` + `test_block_filler` 全部 OK。

### 2. 容器健康检查

```bash
docker compose up -d contract-generator
curl -sf http://localhost:8030/health
curl -sf http://localhost:8030/api/templates | jq .
```

### 3. Preview（不写飞书）

```bash
curl -s -X POST http://localhost:8030/api/generate/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "signing_unit": "趣加旅社",
    "fields": {
      "订单号": "TEST-001",
      "单位": "杭州某某科技有限公司",
      "合同价款": 80000,
      "执行日期": "2026/06/25"
    }
  }' | jq .
```

核对 `placeholders` 与 `missing_required`。

### 4. 真实飞书 POC（需准备模板）

准备清单：

1. 应用权限：`docs:document:copy`、`docx:document`、`drive:drive`
2. 机器人加入模板文件夹（读）与输出文件夹（写）
3. 创建 POC 模板，正文含 `{{甲方名称}}` 等（每个占位符同一 text_run）
4. 记下 URL 中的 `template_token` 与文件夹 `folder_token`

取 token：

```bash
export FEISHU_TOKEN=$(curl -s -X POST \
  'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  -H 'Content-Type: application/json' \
  -d "{\"app_id\":\"$FEISHU_APP_ID\",\"app_secret\":\"$FEISHU_APP_SECRET\"}" \
  | jq -r '.tenant_access_token')
```

探测占位符：

```bash
curl -s -X POST http://localhost:8030/api/probe \
  -H 'Content-Type: application/json' \
  -d "{\"tenant_access_token\":\"$FEISHU_TOKEN\",\"template_token\":\"$TEMPLATE_TOKEN\"}" \
  | jq .
```

一键冒烟：

```bash
export TEMPLATE_TOKEN=doxcnXXXXXXXX
export FOLDER_TOKEN=fldcnXXXXXXXX
./scripts/smoke_generate.sh
```

### 5. 验收标准

| # | 项 | 通过标准 |
| --- | --- | --- |
| 1 | copy | 输出文件夹出现新文档 |
| 2 | 替换 | 打开文档，`{{}}` 消失且值正确 |
| 3 | 样式 | 标题加粗 / 表格边框未破坏 |
| 4 | replaced_blocks | ≥ 1（有占位符被改到的块数） |
| 5 | unresolved | 提供齐全时为空 |
| 6 | 限流 | 连续 3 次 generate 不因 1061045/99991400 失败 |
| 7 | 缺字段 | 少传必填项返回 `MISSING_REQUIRED_PLACEHOLDER` |

---

## 常见问题

| 现象 | 处理 |
| --- | --- |
| copy 403 / `FEISHU_FORBIDDEN` | 给机器人加文件夹协作者 |
| 占位符未替换 | 跑 `/api/probe`；检查是否跨 text_run；核对 key 名 |
| 429 / rate limited | 服务已内置重试；降低并发，n8n 用 batchSize=1 |
| preview 有 missing_required | 补全 `fields` 或改 `config.yaml` 必填列表 |

---

## 后续（本阶段不做）

- n8n 工作流联调、`feishu-listener` 路由
- 路径 C（本地 DOCX + docxtpl）
- 导出 PDF 回写多维表格附件字段
