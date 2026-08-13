#!/usr/bin/env bash
# Smoke-test online quote spreadsheet generation (feishu-files-generation).
#
# Usage:
#   export FEISHU_TOKEN=t-xxx   # or rely on FEISHU_APP_ID/SECRET in the service
#   ./scripts/smoke_online_quote.sh
#
# Optional:
#   BASE_URL=http://127.0.0.1:8030
#   ORDER_NO=smoke-001

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8030}"
ORDER_NO="${ORDER_NO:-smoke-$(date +%Y%m%d%H%M%S)}"

TOKEN_JSON_FIELD=""
if [[ -n "${FEISHU_TOKEN:-}" ]]; then
  TOKEN_JSON_FIELD="\"tenant_access_token\": \"${FEISHU_TOKEN}\","
fi

# Optional: OUTPUT_FOLDER=在线报价  (config.yaml output_folders alias)
OUTPUT_FOLDER_JSON=""
if [[ -n "${OUTPUT_FOLDER:-}" ]]; then
  OUTPUT_FOLDER_JSON="\"output_folder\": \"${OUTPUT_FOLDER}\","
fi

BODY=$(cat <<EOF
{
  ${TOKEN_JSON_FIELD}
  "signing_unit": "在线报价",
  ${OUTPUT_FOLDER_JSON}
  "fields": {
    "订单号": "${ORDER_NO}",
    "活动名称": "冒烟测试团建",
    "报价人数": "30",
    "活动天数": "2",
    "活动日期": "2026年7月1日",
    "出发地目的地": "杭州-安吉"
  },
  "sheet_rows": [
    {"类目": "活动选配", "物品名称": "DAY1团建游戏", "描述": "专业教练", "数量": 1, "单价": 1000},
    {"类目": "用餐安排", "物品名称": "DAY1午餐", "描述": "自助", "数量": 30, "单价": 68},
    {"类目": "交通", "物品名称": "大巴车", "描述": "往返", "数量": 1, "单价": 4800}
  ],
  "itinerary_rows": [
    {"日期": "d1", "时间": "09:00-10:00", "内容": "集合出发"},
    {"日期": "d1", "时间": "12:00-13:00", "内容": "午餐"}
  ]
}
EOF
)

echo "POST ${BASE_URL}/api/generate (signing_unit=在线报价)"
curl -sS -X POST "${BASE_URL}/api/generate" \
  -H 'Content-Type: application/json' \
  -d "${BODY}" | python3 -m json.tool
