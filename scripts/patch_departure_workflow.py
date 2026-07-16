#!/usr/bin/env python3
"""Patch n8n workflow 测试：出团计划单生成 with aggregate + generate + writeback."""

from __future__ import annotations

import json
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

DB = Path("/Users/xuyucheng/My_project/n8n/n8n-data/database.sqlite")
WF_ID = "KELX69kBerHLeSnW"
EXPORT = Path("/Users/xuyucheng/My_project/n8n/files/departure-plan.workflow.json")

AGGREGATE_JS = r'''const quote = $input.first().json;
const loopItem = $('Loop Over Items').item.json;
const execFields = loopItem.fields || {};
const quoteResp = $('查询待识报价附件').item.json;
const quoteFields = quoteResp?.data?.items?.[0]?.fields || {};
const orderRecordId = quoteResp?.data?.items?.[0]?.record_id || '';
const execRecordId = loopItem.record_id || '';
const token = $('获取飞书Token').first().json.tenant_access_token || '';

function textOf(v) {
  if (v == null || v === '') return '';
  if (typeof v === 'string' || typeof v === 'number') return String(v).trim();
  if (Array.isArray(v)) return v.map(textOf).filter(Boolean).join('；');
  if (typeof v === 'object') {
    if (Array.isArray(v.value)) return textOf(v.value);
    if (v.text != null) return String(v.text).trim();
    if (v.name != null) return String(v.name).trim();
  }
  return '';
}

function dateOf(v) {
  if (v == null || v === '') return '';
  if (typeof v === 'number') return new Date(v).toISOString().slice(0, 10).replace(/-/g, '/');
  if (typeof v === 'object' && Array.isArray(v.value) && typeof v.value[0] === 'number') {
    return new Date(v.value[0]).toISOString().slice(0, 10).replace(/-/g, '/');
  }
  return textOf(v).replace(/-/g, '/');
}

const customerRaw = textOf(execFields['客户信息']);
const parts = customerRaw.split(/[；;]/).map((s) => s.trim()).filter(Boolean);
const 单位 = parts[0] || textOf(quoteFields['单位']);
const 联系人 = parts[1] || textOf(quoteFields['联系人']);
const 联系方式 = parts[2] || textOf(quoteFields['联系方式']);

const sheet_rows = (quote.报价明细条目 || []).map((item) => ({
  名称: item.名称 || '',
  描述: item.描述 || '',
  数量: item.数量,
  单价: item.单价,
  结算方式: item.结算方式 || '',
  联系人: item.联系人 || '',
}));

return [
  {
    json: {
      tenant_access_token: token,
      signing_unit: '出团计划单',
      fields: {
        订单号: textOf(quoteFields['订单号']),
        单位,
        联系人,
        联系方式,
        客户领队: '',
        导游: '',
        车型: '',
        车牌: '',
        司机: '',
        教练: '',
        物料: '',
        策划师: textOf(execFields['策划']) || textOf(quoteFields['策划师']),
        执行日期:
          dateOf(quoteFields['执行日期']) ||
          dateOf(execFields['执行日期']) ||
          quote.执行日期_报价单 ||
          '',
        执行人数: quote.人数 || textOf(quoteFields['执行人数']),
        活动名称: quote.活动名称 || '',
        活动行程: quote.活动行程 || '',
        报价明细条目: quote.报价明细条目 || [],
        执行编号: textOf(execFields['执行编号']),
        执行记录_id: execRecordId,
        订单记录_id: orderRecordId,
      },
      sheet_rows,
    },
  },
];
'''

WRITEBACK_JS = r'''const gen = $input.first().json;
const loopItem = $('Loop Over Items').item.json;
const recordId = loopItem.record_id || '';
const orderId = $('出团信息聚合').first().json?.fields?.订单记录_id || '';

if (!gen.success) {
  return [{
    json: {
      fields: {
        执行单生成状态: '生成失败',
      },
      _meta: {
        record_id: recordId,
        order_record_id: orderId,
        error: gen.message || gen.error_code || 'generate failed',
      },
    },
  }];
}

// 执行表状态回写；文档 URL 留在 _meta，便于执行日志查看。
// 若已在执行表新增超链接字段「出团计划」，可取消下方注释。
const fields = {
  执行单生成状态: '已生成',
};
// fields['出团计划'] = { text: gen.document_name || '出团计划单', link: gen.document_url };

return [{
  json: {
    fields,
    _meta: {
      record_id: recordId,
      order_record_id: orderId,
      document_url: gen.document_url,
      sheet_rows_written: gen.sheet_rows_written || 0,
    },
  },
}];
'''


def new_id() -> str:
    return str(uuid.uuid4())


def main() -> None:
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT nodes, connections, settings FROM workflow_entity WHERE id=?",
        (WF_ID,),
    ).fetchone()
    if not row:
        raise SystemExit(f"workflow {WF_ID} not found")

    nodes = json.loads(row[0])
    connections = json.loads(row[1])

    # Update query filter: 合同生成=成功
    for n in nodes:
        if n["name"] == "查询待识别执行记录":
            n["parameters"]["jsonBody"] = json.dumps(
            {
                "filter": {
                    "conjunction": "or",
                    "conditions": [
                        {
                            "field_name": "执行单生成状态",
                            "operator": "is",
                            "value": ["待生成"],
                        },
                        {
                            "field_name": "执行单生成状态",
                            "operator": "is",
                            "value": ["需重试"],
                        },
                    ],
                }
            },
                ensure_ascii=False,
            )

    # Remove if already patched
    keep_names = {
        "When clicking ‘Execute workflow’",
        "获取飞书Token",
        "查询待识别执行记录",
        "查询待识报价附件",
        "获取临时下载链接",
        "Extract from File1",
        "下载文件",
        "Loop Over Items",
        "报价信息提取",
        "拆分item",
        "Webhook",
    }
    nodes = [n for n in nodes if n["name"] in keep_names or n["name"] in {
        "出团信息聚合", "生成出团计划单", "出团回写处理", "回写出团结果"
    }]
    # Drop previous patch nodes to re-add cleanly
    nodes = [n for n in nodes if n["name"] not in {
        "出团信息聚合", "生成出团计划单", "出团回写处理", "回写出团结果", "Webhook"
    }]

    # Positions relative to 报价信息提取
    base_x = 1200
    base_y = -272
    for n in nodes:
        if n["name"] == "报价信息提取":
            base_x = n["position"][0]
            base_y = n["position"][1]

    webhook = {
        "parameters": {
            "httpMethod": "POST",
            "path": "make-departure-plan",
            "options": {},
        },
        "id": new_id(),
        "name": "Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [-1024, 80],
        "webhookId": new_id(),
    }

    aggregate = {
        "parameters": {"jsCode": AGGREGATE_JS},
        "id": new_id(),
        "name": "出团信息聚合",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [base_x + 220, base_y],
    }
    generate = {
        "parameters": {
            "method": "POST",
            "url": "http://contract-generator:8030/api/generate",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json) }}",
            "options": {},
        },
        "id": new_id(),
        "name": "生成出团计划单",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [base_x + 440, base_y],
    }
    writeback_prep = {
        "parameters": {"jsCode": WRITEBACK_JS},
        "id": new_id(),
        "name": "出团回写处理",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [base_x + 660, base_y],
    }
    writeback = {
        "parameters": {
            "method": "PUT",
            "url": '=https://open.feishu.cn/open-apis/bitable/v1/apps/{{ $env.FEISHU_BASE_ID }}/tables/tbl821KbTBpjjyXI/records/{{ $json._meta.record_id }}',
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {
                        "name": "Authorization",
                        "value": '=Bearer {{ $node["获取飞书Token"].json["tenant_access_token"] }}',
                    },
                    {"name": "Content-Type", "value": "application/json"},
                ]
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ fields: $json.fields }) }}",
            "options": {},
        },
        "id": new_id(),
        "name": "回写出团结果",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [base_x + 880, base_y],
    }

    nodes.extend([webhook, aggregate, generate, writeback_prep, writeback])

    connections = {
        "When clicking ‘Execute workflow’": {
            "main": [[{"node": "获取飞书Token", "type": "main", "index": 0}]]
        },
        "Webhook": {
            "main": [[{"node": "获取飞书Token", "type": "main", "index": 0}]]
        },
        "获取飞书Token": {
            "main": [[{"node": "查询待识别执行记录", "type": "main", "index": 0}]]
        },
        "查询待识别执行记录": {
            "main": [[{"node": "拆分item", "type": "main", "index": 0}]]
        },
        "拆分item": {
            "main": [[{"node": "Loop Over Items", "type": "main", "index": 0}]]
        },
        "Loop Over Items": {
            "main": [[{"node": "查询待识报价附件", "type": "main", "index": 0}]]
        },
        "查询待识报价附件": {
            "main": [[{"node": "获取临时下载链接", "type": "main", "index": 0}]]
        },
        "获取临时下载链接": {
            "main": [[{"node": "下载文件", "type": "main", "index": 0}]]
        },
        "下载文件": {
            "main": [[{"node": "Extract from File1", "type": "main", "index": 0}]]
        },
        "Extract from File1": {
            "main": [[{"node": "报价信息提取", "type": "main", "index": 0}]]
        },
        "报价信息提取": {
            "main": [[{"node": "出团信息聚合", "type": "main", "index": 0}]]
        },
        "出团信息聚合": {
            "main": [[{"node": "生成出团计划单", "type": "main", "index": 0}]]
        },
        "生成出团计划单": {
            "main": [[{"node": "出团回写处理", "type": "main", "index": 0}]]
        },
        "出团回写处理": {
            "main": [[{"node": "回写出团结果", "type": "main", "index": 0}]]
        },
        "回写出团结果": {
            "main": [[{"node": "Loop Over Items", "type": "main", "index": 0}]]
        },
    }

    settings = json.loads(row[2]) if row[2] else {}
    settings["executionOrder"] = settings.get("executionOrder") or "v1"

    conn.execute(
        "UPDATE workflow_entity SET nodes=?, connections=?, settings=?, name=?, updatedAt=CURRENT_TIMESTAMP WHERE id=?",
        (
            json.dumps(nodes, ensure_ascii=False),
            json.dumps(connections, ensure_ascii=False),
            json.dumps(settings, ensure_ascii=False),
            "出团计划单生成",
            WF_ID,
        ),
    )
    conn.commit()

    export = {
        "id": WF_ID,
        "name": "出团计划单生成",
        "active": False,
        "nodes": nodes,
        "connections": connections,
        "settings": settings,
    }
    EXPORT.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"updated workflow {WF_ID}")
    print("nodes:", [n["name"] for n in nodes])
    print(f"exported {EXPORT}")


if __name__ == "__main__":
    main()
