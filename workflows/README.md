# 可导入 n8n 工作流

## 测试-在线报价生成（双模式）

| 文件 | 说明 |
|------|------|
| [`测试-在线报价生成.json`](./测试-在线报价生成.json) | **直接 Import**；Webhook `feishu/quote-test` |
| [`online-quote-mode-ids.md`](./online-quote-mode-ids.md) | 生成方式 field/opt 占位符说明 |
| [`../scripts/upgrade_online_quote_dual_mode.js`](../scripts/upgrade_online_quote_dual_mode.js) | 从底稿+提示词重生双模式节点 |
| [`../promet/online-quote-ondemand-system.md`](../promet/online-quote-ondemand-system.md) | 按需生成系统提示词（先审核） |

```bash
node scripts/upgrade_online_quote_dual_mode.js
```

### 导入步骤

1. n8n → 停用旧「测试-在线报价生成」（同一 path：`feishu/quote-test`）
2. Import 本目录 JSON
3. 检查凭证：DeepSeek account；「判定生成方式」里 `MODE_CONFIG` 换成真实 field/opt id
4. 订单需填写「报价生成方式」= 附件生成 / 按需生成，并将「报价生成状态」置为待生成

## 合同校验重新识别（升级）

| 文件 | 说明 |
|------|------|
| [`合同校验重新识别-升级.workflow.json`](./合同校验重新识别-升级.workflow.json) | **直接 Import** |
| [`_base_contract_reid.json`](./_base_contract_reid.json) | 构建用底稿（现网导出） |
| [`../scripts/contract_reid_upgrade_workflow.js`](../scripts/contract_reid_upgrade_workflow.js) | 重新生成脚本 |

```bash
node scripts/contract_reid_upgrade_workflow.js
```

### 导入步骤

1. n8n → 停用旧工作流「合同校验重新识别」（同一 webhook path：`Bill-Re-identification`）
2. Workflows → Import from File → 选择本目录 JSON
3. 检查凭证：DeepSeek account、Header Auth account（豆包）
4. 激活「合同校验重新识别（升级）」

设计说明：`docs/superpowers/specs/2026-08-03-contract-reidentification-upgrade-design.md`

## 合同生成与执行信息填写（在线报价）

| 文件 | 说明 |
|------|------|
| [`合同生成与执行信息填写-在线报价.json`](./合同生成与执行信息填写-在线报价.json) | **直接 Import**；Webhook `make-contract-online` |
| [`合同生成与执行信息填写.json`](./合同生成与执行信息填写.json) | 旧版底稿（附件报价） |

### 数据链路

执行记录 → 关联订单「在线报价」链接 → `sheets/v3/.../sheets/query` → `sheets/v2/.../values_batch_get`（`UnformattedValue`）→ 提取合同字段 → 生成合同 → 回写

### 导入步骤

1. n8n → 停用旧「合同生成与执行信息填写」（旧 path：`make-contract`）
2. Import 本目录 **`合同生成与执行信息填写-在线报价.json`**
3. 确认订单已有「在线报价」超链接，且表格已填单价（活动总价公式有值）
4. 激活新工作流
