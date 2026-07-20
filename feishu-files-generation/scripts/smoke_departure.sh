#!/usr/bin/env bash
# Smoke: preview / generate 出团计划单（需配置 FEISHU_DEPARTURE_* 与 token）
set -euo pipefail

BASE_URL="${FEISHU_FILES_GENERATION_URL:-http://127.0.0.1:8030}"
FEISHU_TOKEN="${FEISHU_TOKEN:-}"

if [[ -z "${FEISHU_TOKEN}" ]]; then
  echo "Set FEISHU_TOKEN (tenant_access_token)" >&2
  exit 1
fi

PAYLOAD=$(cat <<EOF
{
  "tenant_access_token": "${FEISHU_TOKEN}",
  "signing_unit": "出团计划单",
  "fields": {
    "订单号": "DEP-SMOKE-001",
    "单位": "杭州测试科技有限公司",
    "执行日期": "2026/07/14",
    "执行人数": "52",
    "活动名称": "安吉两天一夜团建",
    "活动行程": "day1\\t10:00-12:00\\t前往景区\\nday1\\t12:00-13:00\\t午餐",
    "客户领队": "",
    "导游": "",
    "车型": "",
    "车牌": "",
    "司机": "",
    "教练": "",
    "物料": "",
    "策划师": "测试策划",
    "报价明细条目": [
      {"名称": "大巴车（55座）", "描述": "接送", "数量": 1, "单价": 4800},
      {"名称": "保险费用", "描述": "意外险", "数量": 52, "单价": 10}
    ]
  },
  "options": {"dry_run": true}
}
EOF
)

echo "== preview =="
curl -sS -X POST "${BASE_URL}/api/generate/preview" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}" | python3 -m json.tool

if [[ "${RUN_LIVE:-0}" == "1" ]]; then
  LIVE=$(echo "${PAYLOAD}" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["options"]={"dry_run":False}; print(json.dumps(d,ensure_ascii=False))')
  echo "== generate =="
  curl -sS -X POST "${BASE_URL}/api/generate" \
    -H "Content-Type: application/json" \
    -d "${LIVE}" | python3 -m json.tool
fi
