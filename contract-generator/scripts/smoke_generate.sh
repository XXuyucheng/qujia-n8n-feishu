#!/usr/bin/env bash
# Smoke test: call /api/generate with explicit template tokens.
# Required env:
#   FEISHU_TOKEN              tenant_access_token
#   TEMPLATE_TOKEN            Feishu docx template token
#   FOLDER_TOKEN              output folder token
# Optional:
#   BASE_URL                  default http://localhost:8030

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8030}"
FEISHU_TOKEN="t-g10479foLTBYIR4WLY3QGOD3L7PGM7252EUTQMT7"
TEMPLATE_TOKEN="QyyUdUdXYoXzklxSUUGcanmenoh"
FOLDER_TOKEN="UtBAfoddYlwW3tdA6FTcwiImn1c"
DOC_NAME="${DOC_NAME:-POC-合同-$(date +%Y%m%d%H%M%S)}"

echo "==> health"
curl -sf "${BASE_URL}/health" | tee /dev/stderr
echo

echo "==> probe"
curl -sf -X POST "${BASE_URL}/api/probe" \
  -H 'Content-Type: application/json' \
  -d "{
    \"tenant_access_token\": \"${FEISHU_TOKEN}\",
    \"template_token\": \"${TEMPLATE_TOKEN}\"
  }" | tee /dev/stderr
echo

echo "==> generate ${DOC_NAME}"
curl -sf -X POST "${BASE_URL}/api/generate" \
  -H 'Content-Type: application/json' \
  -d "{
    \"tenant_access_token\": \"${FEISHU_TOKEN}\",
    \"template_token\": \"${TEMPLATE_TOKEN}\",
    \"folder_token\": \"${FOLDER_TOKEN}\",
    \"document_name\": \"${DOC_NAME}\",
    \"placeholders\": {
      \"甲方名称\": \"杭州某某科技有限公司\",
      \"甲方联系人\": \"张三\",
      \"甲方电话\": \"13800138000\",
      \"乙方名称\": \"杭州趣加旅社有限公司\",
      \"活动日期\": \"2026/06/25\",
      \"活动人数\": \"120\",
      \"活动描述\": \"岱山两日团建\",
      \"合同价款\": \"80,000.00\",
      \"订单号\": \"POC-001\",
      \"策划师\": \"阿铭\",
      \"签订日期\": \"2026年07月08日\"
    }
  }" | tee /dev/stderr
echo
echo "==> done"
