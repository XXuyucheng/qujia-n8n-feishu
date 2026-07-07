#!/usr/bin/env python3
"""Upgrade n8n invoice recognition workflow for multi-attachment merge."""

import json
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIG = ROOT / "files" / "_invoice_workflow_orig.json"
OUT = ROOT / "files" / "invoice-recognition.workflow.json"
DB = ROOT / "n8n-data" / "database.sqlite"
WF_ID = "pkVuecTu7UXNzWnA"

EXPAND_ATTACHMENTS_CODE = r"""function unwrapAttachmentField(field) {
  if (Array.isArray(field)) return field;
  if (field && Array.isArray(field.value)) return field.value;
  return [];
}

function detectPdfStatus(item) {
  if (!item || typeof item !== 'object') return 'unknown';

  const name = String(item.name || item.text || item.title || '').trim().toLowerCase();
  const type = String(item.type || item.mime_type || item.mimeType || '').trim().toLowerCase();

  if (name.endsWith('.pdf')) return 'pdf';
  if (type.includes('pdf') || type === 'application/pdf') return 'pdf';

  const nonPdfExt = ['.doc', '.docx', '.xls', '.xlsx', '.jpg', '.jpeg', '.png'];
  if (nonPdfExt.some((ext) => name.endsWith(ext))) return 'non_pdf';

  return 'unknown';
}

const record = $('循环处理发票').item.json;
const recordId = record.record_id || '';
const attachments = unwrapAttachmentField((record.fields || {})['发票']);
const pdfs = attachments.filter((item) => detectPdfStatus(item) === 'pdf');

if (!pdfs.length) {
  return [
    {
      json: {
        skip: true,
        record_id: recordId,
        reason: '无发票PDF附件',
      },
    },
  ];
}

return pdfs.map((item, index) => ({
  json: {
    skip: false,
    record_id: recordId,
    attachment_index: index,
    attachment_total: pdfs.length,
    attachment_name: String(item.name || item.text || item.title || '').trim(),
    tmp_url: item.tmp_url || '',
  },
}));
"""

MERGE_RESULTS_CODE = r"""const recordId = $('循环处理发票').first().json.record_id;
const items = $('PDF解析结果标准化').all()
  .map((i) => i.json)
  .filter((i) => i.record_id === recordId)
  .sort((a, b) => (a.attachment_index || 0) - (b.attachment_index || 0));

if (!items.length) {
  return [
    {
      json: {
        mergeStatus: 'error',
        record_id: recordId,
        失败原因: '未获取到解析结果',
      },
    },
  ];
}

if (items.some((i) => !i.字段完整 || i.解析失败)) {
  const reasons = items
    .filter((i) => !i.字段完整)
    .map((i) => {
      const name = i.attachment_name ? `[${i.attachment_name}]` : '';
      return `${name}${i.失败原因 || '识别字段不完整'}`.trim();
    })
    .filter(Boolean);

  return [
    {
      json: {
        mergeStatus: 'error',
        record_id: recordId,
        失败原因: reasons.join('；') || '部分发票识别字段不完整',
      },
    },
  ];
}

if (items.length === 1) {
  const one = { ...items[0] };
  delete one.attachment_index;
  delete one.attachment_name;
  delete one.attachment_total;
  return [{ json: { mergeStatus: 'ok', ...one } }];
}

const buyer = items[0].购方名称;
const seller = items[0].销方名称;
const partiesMatch = items.every((i) => i.购方名称 === buyer && i.销方名称 === seller);

if (!partiesMatch) {
  return [
    {
      json: {
        mergeStatus: 'merge_error',
        record_id: recordId,
        购方名称: '🔔发票附件中，有多张发票合并识别的，发票必须销售方和购买方一致。',
      },
    },
  ];
}

const total = items.reduce((sum, i) => sum + Number(i.价税合计 || 0), 0);
const dates = items.map((i) => i.开票日期).filter(Boolean).sort();

return [
  {
    json: {
      mergeStatus: 'ok',
      record_id: recordId,
      发票号码: items.map((i) => i.发票号码).join('，'),
      开票日期: dates[0] || '',
      购方名称: buyer,
      销方名称: seller,
      价税合计: total.toFixed(2),
      合并张数: items.length,
    },
  },
];
"""

PREP_FEISHU_CODE = r"""function cleanString(v) {
  if (v === null || v === undefined) return '';
  return String(v).trim();
}

function cleanAmount(v) {
  if (v === null || v === undefined || v === '') return null;

  const s = String(v)
    .replace(/[,，¥￥元\s]/g, '')
    .trim();

  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

function dateToFeishuTimestamp(dateStr) {
  if (!dateStr) return null;

  const s = String(dateStr).trim();
  const m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (!m) return null;

  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const dt = new Date(y, mo - 1, d, 0, 0, 0, 0);
  return dt.getTime();
}

if ($json.mergeStatus === 'merge_error') {
  return [
    {
      json: {
        feishu_ready: true,
        feishu_update_body: {
          fields: {
            识别状态: '错误合并',
            购方名称: $json.购方名称,
          },
        },
      },
    },
  ];
}

const invoiceNo = cleanString($json['发票号码']);
const invoiceDate = cleanString($json['开票日期']);
const buyerName = cleanString($json['购方名称']);
const sellerName = cleanString($json['销方名称']);
const totalAmount = cleanAmount($json['价税合计']);

const dateTimestamp = dateToFeishuTimestamp(invoiceDate);

const missing = [
  !invoiceNo ? '缺少发票号码' : '',
  !invoiceDate ? '缺少开票日期' : '',
  !dateTimestamp ? '开票日期格式无效' : '',
  !buyerName ? '缺少购方名称' : '',
  !sellerName ? '缺少销方名称' : '',
  totalAmount === null ? '价税合计格式无效' : '',
].filter(Boolean);

if (missing.length > 0) {
  return [
    {
      json: {
        ...$json,
        mergeStatus: 'error',
        feishu_ready: false,
        失败原因: missing.join('；'),
      },
    },
  ];
}

const fields = {
  发票号码: invoiceNo,
  开票日期: dateTimestamp,
  购方名称: buyerName,
  销方名称: sellerName,
  价税合计: totalAmount,
  识别状态: '已识别',
};

return [
  {
    json: {
      ...$json,
      feishu_ready: true,
      feishu_fields: fields,
      feishu_update_body: {
        fields,
      },
    },
  },
];
"""

PDF_NORM_SUFFIX = r"""
const attachCtx = $('循环处理附件').item.json;
const withContext = {
  ...normalized,
  record_id: attachCtx.record_id,
  attachment_index: attachCtx.attachment_index,
  attachment_name: attachCtx.attachment_name,
};

return [{ json: withContext }];
"""


def new_id() -> str:
    return str(uuid.uuid4())


def node(name, ntype, position, parameters, type_version=None, **extra):
    versions = {
        "n8n-nodes-base.code": 2,
        "n8n-nodes-base.if": 2.3,
        "n8n-nodes-base.splitInBatches": 3,
        "n8n-nodes-base.httpRequest": 4.4,
    }
    return {
        "parameters": parameters,
        "type": ntype,
        "typeVersion": type_version or versions.get(ntype, 1),
        "position": position,
        "id": new_id(),
        "name": name,
        **extra,
    }


def patch_pdf_normalize_code(original: str) -> str:
    marker = "return [\n  {\n    json: normalized,\n  },\n];"
    failure_marker = "return [\n      {\n        json: {\n          解析失败: true,"
    if marker not in original:
        raise ValueError("PDF normalize return marker not found")

    patched = original.replace(
        marker,
        PDF_NORM_SUFFIX.strip(),
        1,
    )

    failure_return = """return [
      {
        json: {
          解析失败: true,
          字段完整: false,
          失败原因: '无法解析 JSON：既没有 PDF Parser fields，也没有合法 AI JSON',
          原始内容: raw,
          source,
          record_id: $('循环处理附件').item.json.record_id,
          attachment_index: $('循环处理附件').item.json.attachment_index,
          attachment_name: $('循环处理附件').item.json.attachment_name,
        },
      },
    ];"""
    patched = patched.replace(
        """return [
      {
        json: {
          解析失败: true,
          字段完整: false,
          失败原因: '无法解析 JSON：既没有 PDF Parser fields，也没有合法 AI JSON',
          原始内容: raw,
          source,
        },
      },
    ];""",
        failure_return,
        1,
    )
    return patched


def build_workflow(orig: dict) -> dict:
    wf = deepcopy(orig)
    nodes_by_name = {n["name"]: n for n in wf["nodes"]}

    # Optional: env-based token request
    token_node = nodes_by_name["获取飞书Token"]
    token_node["parameters"]["jsonBody"] = (
        '={\n  "app_id": "{{ $env.FEISHU_APP_ID }}",\n'
        '  "app_secret": "{{ $env.FEISHU_APP_SECRET }}"\n}'
    )

    nodes_by_name["获取临时下载链接"]["parameters"]["url"] = "={{$json.tmp_url}}"

    nodes_by_name["PDF解析结果标准化"]["parameters"]["jsCode"] = patch_pdf_normalize_code(
        nodes_by_name["PDF解析结果标准化"]["parameters"]["jsCode"]
    )
    nodes_by_name["PDF解析结果标准化"]["parameters"]["mode"] = "runOnceForEachItem"

    nodes_by_name["准备飞书写入数据"]["parameters"]["jsCode"] = PREP_FEISHU_CODE

    nodes_by_name["更新发票识别结果1"]["parameters"]["jsonBody"] = (
        '={\n  "fields": {\n    "识别状态": "错误"\n  }\n}'
    )

    if_node = nodes_by_name["If1"]
    if_node["name"] = "是否可回写?"
    if_node["parameters"] = {
        "conditions": {
            "options": {
                "caseSensitive": True,
                "leftValue": "",
                "typeValidation": "strict",
                "version": 3,
            },
            "conditions": [
                {
                    "id": new_id(),
                    "leftValue": "={{$json.mergeStatus}}",
                    "rightValue": "ok",
                    "operator": {"type": "string", "operation": "equals"},
                },
                {
                    "id": new_id(),
                    "leftValue": "={{$json.mergeStatus}}",
                    "rightValue": "merge_error",
                    "operator": {"type": "string", "operation": "equals"},
                },
            ],
            "combinator": "or",
        },
        "options": {},
    }

    new_nodes = [
        node(
            "01 展开发票附件",
            "n8n-nodes-base.code",
            [1376, 16],
            {"jsCode": EXPAND_ATTACHMENTS_CODE, "mode": "runOnceForEachItem"},
        ),
        node(
            "有PDF附件?",
            "n8n-nodes-base.if",
            [1584, 16],
            {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 3,
                    },
                    "conditions": [
                        {
                            "id": new_id(),
                            "leftValue": "={{$json.skip}}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "false",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
        ),
        node(
            "循环处理附件",
            "n8n-nodes-base.splitInBatches",
            [1792, 16],
            {"options": {}},
        ),
        node(
            "02 合并多发票结果",
            "n8n-nodes-base.code",
            [2160, 160],
            {"jsCode": MERGE_RESULTS_CODE, "mode": "runOnceForAllItems"},
        ),
        node(
            "更新无附件错误",
            "n8n-nodes-base.httpRequest",
            [1792, 240],
            {
                "method": "PUT",
                "url": "=https://open.feishu.cn/open-apis/bitable/v1/apps/Y2ZgbUGWqa0MB4sLmXxc89T5n0P/tables/tblNd9zgN7ALihwj/records/{{$json.record_id}}",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "Authorization",
                            "value": '=Bearer {{$node["获取飞书Token"].json["tenant_access_token"]}}',
                        },
                        {"name": "Content-Type", "value": "application/json"},
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": '={\n  "fields": {\n    "识别状态": "错误"\n  }\n}',
                "options": {},
            },
        ),
        node(
            "飞书数据就绪?",
            "n8n-nodes-base.if",
            [2592, 0],
            {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 3,
                    },
                    "conditions": [
                        {
                            "id": new_id(),
                            "leftValue": "={{$json.feishu_ready}}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
        ),
    ]

    wf["nodes"].extend(new_nodes)

    # Shift downstream nodes right for layout clarity
    for n in wf["nodes"]:
        if n["name"] in {"获取临时下载链接", "下载PDF", "调用PDF解析服务", "PDF解析结果标准化"}:
            n["position"] = [n["position"][0] + 416, n["position"][1]]
        if n["name"] in {"是否可回写?", "准备飞书写入数据", "更新发票识别结果", "更新发票识别结果1"}:
            n["position"] = [n["position"][0] + 208, n["position"][1]]

    wf["connections"] = {
        "查询待识别发票": {"main": [[{"node": "拆分飞书Items", "type": "main", "index": 0}]]},
        "获取飞书Token": {"main": [[{"node": "查询待识别发票", "type": "main", "index": 0}]]},
        "循环处理发票": {
            "main": [
                [],
                [{"node": "更新状态_识别中", "type": "main", "index": 0}],
            ]
        },
        "更新状态_识别中": {
            "main": [[{"node": "01 展开发票附件", "type": "main", "index": 0}]]
        },
        "拆分飞书Items": {"main": [[{"node": "循环处理发票", "type": "main", "index": 0}]]},
        "01 展开发票附件": {"main": [[{"node": "有PDF附件?", "type": "main", "index": 0}]]},
        "有PDF附件?": {
            "main": [
                [{"node": "循环处理附件", "type": "main", "index": 0}],
                [{"node": "更新无附件错误", "type": "main", "index": 0}],
            ]
        },
        "循环处理附件": {
            "main": [
                [{"node": "02 合并多发票结果", "type": "main", "index": 0}],
                [{"node": "获取临时下载链接", "type": "main", "index": 0}],
            ]
        },
        "获取临时下载链接": {"main": [[{"node": "下载PDF", "type": "main", "index": 0}]]},
        "下载PDF": {"main": [[{"node": "调用PDF解析服务", "type": "main", "index": 0}]]},
        "调用PDF解析服务": {
            "main": [[{"node": "PDF解析结果标准化", "type": "main", "index": 0}]]
        },
        "PDF解析结果标准化": {
            "main": [[{"node": "循环处理附件", "type": "main", "index": 0}]]
        },
        "02 合并多发票结果": {
            "main": [[{"node": "是否可回写?", "type": "main", "index": 0}]]
        },
        "是否可回写?": {
            "main": [
                [{"node": "准备飞书写入数据", "type": "main", "index": 0}],
                [{"node": "更新发票识别结果1", "type": "main", "index": 0}],
            ]
        },
        "准备飞书写入数据": {
            "main": [[{"node": "飞书数据就绪?", "type": "main", "index": 0}]]
        },
        "飞书数据就绪?": {
            "main": [
                [{"node": "更新发票识别结果", "type": "main", "index": 0}],
                [{"node": "更新发票识别结果1", "type": "main", "index": 0}],
            ]
        },
        "更新发票识别结果": {
            "main": [[{"node": "循环处理发票", "type": "main", "index": 0}]]
        },
        "更新发票识别结果1": {
            "main": [[{"node": "循环处理发票", "type": "main", "index": 0}]]
        },
        "更新无附件错误": {
            "main": [[{"node": "循环处理发票", "type": "main", "index": 0}]]
        },
        "Feishu Listener Webhook": {
            "main": [[{"node": "获取飞书Token", "type": "main", "index": 0}]]
        },
    }

    return wf


def export_for_n8n(wf: dict) -> dict:
    export = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": wf.get("settings", {}),
        "staticData": wf.get("staticData"),
        "pinData": wf.get("pinData", {}),
    }
    if wf.get("meta"):
        export["meta"] = wf["meta"]
    return export


def update_database(wf: dict) -> None:
    conn = sqlite3.connect(DB)
    conn.execute(
        "UPDATE workflow_entity SET nodes = ?, connections = ?, updatedAt = datetime('now') WHERE id = ?",
        (json.dumps(wf["nodes"], ensure_ascii=False), json.dumps(wf["connections"], ensure_ascii=False), WF_ID),
    )
    conn.commit()
    conn.close()


def main() -> None:
    with ORIG.open() as f:
        orig = json.load(f)

    wf = build_workflow(orig)
    export = export_for_n8n(wf)

    OUT.write_text(json.dumps(export, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_database(wf)

    print(f"Updated workflow in DB: {WF_ID}")
    print(f"Exported: {OUT}")
    print(f"Nodes: {len(wf['nodes'])}")


if __name__ == "__main__":
    main()
