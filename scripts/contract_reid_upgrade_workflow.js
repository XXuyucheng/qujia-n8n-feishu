#!/usr/bin/env node
/**
 * 合同校验重新识别（升级）— 独立工作流构建脚本
 *
 * 用法:
 *   node scripts/contract_reid_upgrade_workflow.js
 *
 * 产出（可自行在 n8n Import from File）:
 *   workflows/合同校验重新识别-升级.workflow.json
 *
 * 不写 n8n-data/database.sqlite。导入前请停用旧「合同校验重新识别」，
 * 避免 webhook path Bill-Re-identification 冲突。
 */

const fs = require('fs');
const path = require('path');
const { randomUUID } = require('crypto');

const ROOT = path.resolve(__dirname, '..');
const BASE_PATH = path.join(ROOT, 'workflows', '_base_contract_reid.json');
const OUT_PATH = path.join(ROOT, 'workflows', '合同校验重新识别-升级.workflow.json');

const DEST_OPTIONS = [
  '南京',
  '安吉',
  '千岛湖',
  '德清',
  '湖州市区',
  '上海内',
  '桐庐',
  '舟山',
  '临安',
  '台州',
  '嘉兴',
  '杭州内',
  '象山',
  '宁波',
  '无锡',
  '苏州',
  '溧阳',
  '合肥',
  '义乌',
  '其他',
];

const DEST_ALIASES = [
  { alias: '湖州市区', target: '湖州市区' },
  { alias: '上海内', target: '上海内' },
  { alias: '杭州内', target: '杭州内' },
  { alias: '千岛湖', target: '千岛湖' },
  { alias: '杭州', target: '杭州内' },
  { alias: '上海', target: '上海内' },
  { alias: '湖州', target: '湖州市区' },
];

/** 纯函数：关键词匹配目的地（可供单测） */
function matchDestinationsByKeyword(activityName, options = DEST_OPTIONS, aliases = DEST_ALIASES) {
  const text = String(activityName || '');
  const hits = new Set();
  const sorted = [...options].sort((a, b) => b.length - a.length);
  for (const opt of sorted) {
    if (opt && text.includes(opt)) hits.add(opt);
  }
  for (const { alias, target } of aliases) {
    if (text.includes(alias) && options.includes(target)) hits.add(target);
  }
  hits.delete('其他');
  return [...hits];
}

const CODE_02 = `function unwrapAttachmentField(field) {
  if (Array.isArray(field)) return field;
  if (field && Array.isArray(field.value)) return field.value;
  return [];
}

function pickText(field) {
  if (field == null) return '';
  if (typeof field === 'string') return field.trim();
  if (typeof field === 'number') return String(field);
  if (Array.isArray(field)) return pickText(field[0]);
  if (typeof field === 'object') {
    if (Array.isArray(field.value) && field.value.length) {
      const v = field.value[0];
      if (typeof v === 'string') return v.trim();
      if (v && typeof v.text === 'string') return v.text.trim();
    }
    if (typeof field.text === 'string') return field.text.trim();
  }
  return '';
}

function pickDate(field) {
  if (field == null) return '';
  if (typeof field === 'number') {
    return new Date(field).toISOString().slice(0, 10);
  }
  if (typeof field === 'object' && Array.isArray(field.value) && field.value.length) {
    const v = field.value[0];
    if (typeof v === 'number') return new Date(v).toISOString().slice(0, 10);
  }
  return pickText(field);
}

function detectPdfStatus(item) {
  if (!item || typeof item !== 'object') return 'unknown';
  const name = String(item.name || item.text || item.title || '').trim().toLowerCase();
  const type = String(item.type || item.mime_type || item.mimeType || '').trim().toLowerCase();
  if (name.endsWith('.pdf')) return 'pdf';
  if (type.includes('pdf') || type === 'application/pdf') return 'pdf';
  const nonPdfExt = ['.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.jpg', '.jpeg', '.png', '.zip', '.rar'];
  if (nonPdfExt.some((ext) => name.endsWith(ext))) return 'non_pdf';
  return 'unknown';
}

function pickContractAttachment(items) {
  if (!items.length) return null;
  const pdfs = items.filter((item) => detectPdfStatus(item) === 'pdf');
  const list = pdfs.length ? pdfs : items;
  return list[list.length - 1];
}

// PDF 大小不再在此硬拦；豆包前按单图 10MB / 请求体 64MB 校验
const ctx = $('01 检查执行记录').item.json;
const order = $('获取关联订单').item.json?.data?.records?.[0]?.fields || {};
const contractAttachments = unwrapAttachmentField(order['合同']);
const picked = pickContractAttachment(contractAttachments);
const pdfStatus = picked ? detectPdfStatus(picked) : 'none';
const contractFileName = picked
  ? String(picked.name || picked.text || picked.title || '').trim()
  : '';
const contractFileSize = Number(picked?.size || picked?.file_size || 0);
const contractFileSizeMB = contractFileSize > 0
  ? Math.round((contractFileSize / 1024 / 1024) * 10) / 10
  : 0;

const hasReference = contractAttachments.length > 0;
const isPdfContract = pdfStatus === 'pdf' || pdfStatus === 'unknown';
const hasOrderContract = hasReference && isPdfContract;

let noContractReason = ctx.noContractReason || '';
if (!hasReference) {
  noContractReason = '关联订单无合同附件';
} else if (pdfStatus === 'non_pdf') {
  noContractReason = \`关联订单合同非PDF格式\${contractFileName ? \`：\${contractFileName}\` : ''}\`;
} else if (pdfStatus === 'none') {
  noContractReason = '关联订单未找到合同附件';
}

return {
  json: {
    ...ctx,
    hasOrderContract,
    isPdfContract,
    pdfStatus,
    contractFileName,
    contractFileSize,
    contractFileSizeMB,
    noContractReason,
    订单合同价款: pickText(order['合同价款']),
    订单执行人数: order['执行人数'] != null ? String(order['执行人数']) : ctx.执行人数,
    订单执行日期: pickDate(order['执行日期']) || ctx.执行日期,
  },
};
`;

const CODE_PREP_STAMP = `const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_BODY_BYTES = 64 * 1024 * 1024;
const MODEL = 'ep-20260702143654-vlssq';

function b64DecodedBytes(b64) {
  const s = String(b64 || '').replace(/\\s/g, '');
  if (!s) return 0;
  const padding = (s.match(/=*$/) || [''])[0].length;
  return Math.floor((s.length * 3) / 4) - padding;
}

function utf8ByteLength(str) {
  return Buffer.byteLength(String(str || ''), 'utf8');
}

// 与字段支路并行：只用执行记录 + 本节点输入（解析服务输出），避免等待准备合同文本
const ctx = $('01 检查执行记录').item.json;
const parser = $json || {};
const stampImages = (parser.stamp_images || [])
  .filter((x) => x?.image_base64)
  .map((x) => ({
    page: x.page,
    image_base64: String(x.image_base64).replace(/\\s/g, ''),
  }))
  .filter((x) => x.image_base64.length > 0);

const baseMeta = {
  record_id: ctx.record_id,
  execNo: ctx.execNo,
  stamp_pages: parser.stamp_pages || [],
};

if (!stampImages.length) {
  return [{
    json: {
      ...baseMeta,
      has_stamp_image: false,
      stamp_page: null,
      image_base64: '',
      skip_doubao: true,
      stamp_size_error: false,
      skip_doubao_reason: '未获取签章页图片',
      doubao_request_body: null,
    },
  }];
}

const ready = [];
const oversized = [];

for (const img of stampImages) {
  const imageBytes = b64DecodedBytes(img.image_base64);
  const bodyObj = {
    model: MODEL,
    input: [{
      role: 'user',
      content: [
        {
          type: 'input_image',
          image_url: 'data:image/png;base64,' + img.image_base64,
        },
        {
          type: 'input_text',
          text: '这是合同第' + img.page + '页。只输出JSON：{"甲方盖章":"是|否|不确定","乙方盖章":"是|否|不确定","甲方签字":"是|否|不确定","甲方身份证复印件":"是|否|不确定","说明":""}',
        },
      ],
    }],
  };
  const bodyStr = JSON.stringify(bodyObj);
  const bodyBytes = utf8ByteLength(bodyStr);

  if (imageBytes > MAX_IMAGE_BYTES) {
    oversized.push({
      page: img.page,
      reason: \`签章图片超过10MB（第\${img.page}页约\${(imageBytes / 1024 / 1024).toFixed(1)}MB）\`,
      imageBytes,
      bodyBytes,
    });
    continue;
  }
  if (bodyBytes > MAX_BODY_BYTES) {
    oversized.push({
      page: img.page,
      reason: \`豆包请求体超过64MB（第\${img.page}页约\${(bodyBytes / 1024 / 1024).toFixed(1)}MB），无法调用\`,
      imageBytes,
      bodyBytes,
    });
    continue;
  }

  ready.push({
    json: {
      ...baseMeta,
      has_stamp_image: true,
      stamp_page: img.page,
      image_base64: img.image_base64,
      skip_doubao: false,
      stamp_size_error: false,
      doubao_request_body: bodyObj,
      image_bytes: imageBytes,
      body_bytes: bodyBytes,
    },
  });
}

if (!ready.length) {
  const reason = oversized.map((x) => x.reason).join('；') || '签章图片无法满足豆包大小限制';
  return [{
    json: {
      ...baseMeta,
      has_stamp_image: true,
      stamp_page: oversized[0]?.page ?? null,
      image_base64: '',
      skip_doubao: true,
      stamp_size_error: true,
      skip_doubao_reason: reason,
      doubao_request_body: null,
    },
  }];
}

return ready;
`;

const CODE_DEST_KEYWORD = `const DEST_OPTIONS = ${JSON.stringify(DEST_OPTIONS, null, 2)};
const DEST_ALIASES = ${JSON.stringify(DEST_ALIASES, null, 2)};

function matchDestinationsByKeyword(activityName) {
  const text = String(activityName || '');
  const hits = new Set();
  const sorted = [...DEST_OPTIONS].sort((a, b) => b.length - a.length);
  for (const opt of sorted) {
    if (opt && text.includes(opt)) hits.add(opt);
  }
  for (const { alias, target } of DEST_ALIASES) {
    if (text.includes(alias) && DEST_OPTIONS.includes(target)) hits.add(target);
  }
  hits.delete('其他');
  return [...hits];
}

const d = { ...$json };
const keywordHits = matchDestinationsByKeyword(d.活动名称);
const need_dest_llm = keywordHits.length === 0 && Boolean(String(d.活动名称 || '').trim());

return {
  json: {
    ...d,
    目的地: keywordHits,
    dest_source: keywordHits.length ? 'keyword' : '',
    need_dest_llm,
    DEST_OPTIONS,
  },
};
`;

const CODE_DEST_MERGE_LLM = `const base = $('目的地关键词匹配').first().json;
const raw = $json.output ?? $json.text ?? $json.response ?? '';
const allowed = new Set(base.DEST_OPTIONS || []);

let picked = [];
try {
  const cleaned = String(raw).replace(/\`\`\`json/g, '').replace(/\`\`\`/g, '').trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start >= 0 && end > start) {
    const data = JSON.parse(cleaned.slice(start, end + 1));
    const arr = data['目的地'] || data.destinations || [];
    if (Array.isArray(arr)) {
      picked = arr.map((x) => String(x || '').trim()).filter((x) => allowed.has(x) && x !== '其他');
    }
  }
} catch (e) {
  picked = [];
}

picked = [...new Set(picked)];

return {
  json: {
    ...base,
    目的地: picked,
    dest_source: picked.length ? 'deepseek' : '',
    need_dest_llm: false,
  },
};
`;

const CODE_BUILD_WRITE_A = `const d = $json;

let 预付款比例校验 = '失败';
const rawPayWarning = d.付款比例是否预警;
if (rawPayWarning === true || rawPayWarning === '是' || String(rawPayWarning).toLowerCase() === 'true') {
  预付款比例校验 = '预警';
} else if (rawPayWarning === false || rawPayWarning === '否' || String(rawPayWarning).toLowerCase() === 'false') {
  预付款比例校验 = '正常';
}

const destinations = Array.isArray(d.目的地) ? d.目的地.filter(Boolean) : [];
const fields = { 预付款比例校验 };
if (destinations.length) {
  fields['目的地'] = destinations;
}

return {
  json: {
    branch: 'fields',
    record_id: d.record_id,
    execNo: d.execNo,
    活动名称: d.活动名称 || '',
    活动日期: d.活动日期 || '',
    活动人数: d.活动人数 || '',
    活动总费用: d.活动总费用 || '',
    付款比例和方式: d.付款比例和方式 || '',
    付款比例是否预警: d.付款比例是否预警,
    字段识别说明: d.字段识别说明 || '',
    目的地: destinations,
    dest_source: d.dest_source || '',
    预付款比例校验,
    fields_ok: Boolean(d.活动名称 || d.活动日期 || d.活动人数 || d.活动总费用 || d.付款比例和方式),
    feishu_update_body_a: { fields },
  },
};
`;

const CODE_STAMP_PASS = `const j = { ...$json };
return {
  json: {
    branch: 'stamp',
    record_id: j.record_id,
    execNo: j.execNo,
    双方盖章: j.双方盖章 || '不确定',
    甲方盖章: j.甲方盖章 || '不确定',
    乙方盖章: j.乙方盖章 || '不确定',
    甲方签字: j.甲方签字 || '不确定',
    甲方身份证复印件: j.甲方身份证复印件 || '不确定',
    签字合同状态: j.签字合同状态 || '不适用',
    盖章说明: j.盖章说明 || '',
    签章页识别明细: j.签章页识别明细 || [],
    stamp_size_error: Boolean(j.stamp_size_error),
    stamp_ok: !j.stamp_size_error && (j.双方盖章 === '是' || j.甲方盖章 === '是' || j.乙方盖章 === '是'),
  },
};
`;

const CODE_STAMP_SKIP = `const prep = $('准备签章图片').first().json;
const reason = prep.skip_doubao_reason || '未获取签章页图片';
return {
  json: {
    branch: 'stamp',
    record_id: prep.record_id,
    execNo: prep.execNo,
    双方盖章: '不确定',
    甲方盖章: '不确定',
    乙方盖章: '不确定',
    甲方签字: '不确定',
    甲方身份证复印件: '不确定',
    签字合同状态: '不适用',
    盖章说明: reason,
    签章页识别明细: [],
    stamp_size_error: Boolean(prep.stamp_size_error),
    stamp_ok: false,
  },
};
`;

const CODE_PARSE_STAMP = `const ctx = $('01 检查执行记录').item.json;
const prepItems = $('准备签章图片').all();
const doubaoItems = $input.all();

const base = {
  record_id: ctx.record_id,
  execNo: ctx.execNo,
  stamp_pages: prepItems[0]?.json?.stamp_pages || [],
};

const defaults = {
  双方盖章: '不确定',
  甲方盖章: '不确定',
  乙方盖章: '不确定',
  甲方签字: '不确定',
  甲方身份证复印件: '不确定',
  签字合同状态: '不适用',
  盖章说明: '',
  签章页识别明细: [],
};

function hasDoubaoResponse(item) {
  const j = item?.json || {};
  if (j.error || j.message?.includes?.('error')) return false;
  return Array.isArray(j.output) || typeof j.output_text === 'string';
}

if (!doubaoItems.length || !doubaoItems.some(hasDoubaoResponse)) {
  const skipReason = prepItems[0]?.json?.skip_doubao_reason || '豆包盖章识别失败或无结果';
  return {
    json: {
      ...base,
      ...defaults,
      盖章说明: skipReason,
      stamp_size_error: false,
    },
  };
}

function extractText(res) {
  if (typeof res?.output_text === 'string' && res.output_text.trim()) {
    return res.output_text.trim();
  }
  const chunks = [];
  for (const item of res?.output || []) {
    if (item?.type === 'message' && Array.isArray(item.content)) {
      for (const part of item.content) {
        if (typeof part?.text === 'string' && part.text.trim()) chunks.push(part.text.trim());
      }
    }
    if (item?.type === 'reasoning' && Array.isArray(item.summary)) {
      for (const part of item.summary) {
        if (typeof part?.text === 'string' && part.text.trim()) chunks.push(part.text.trim());
      }
    }
  }
  return chunks.join('\\n');
}

function parseStampJson(raw) {
  const cleaned = String(raw).replace(/\`\`\`json/g, '').replace(/\`\`\`/g, '').trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(cleaned.slice(start, end + 1));
  } catch (_) {
    return null;
  }
}

function normalizeYesNo(value) {
  const v = String(value || '').trim();
  if (v === '是' || v === '否' || v === '不确定') return v;
  return '不确定';
}

function evaluateSignContract(甲方签字, 甲方身份证复印件, 甲方盖章) {
  if (甲方签字 === '是') {
    if (甲方盖章 === '是' || 甲方身份证复印件 === '是') return '有效';
    return '无效签字合同';
  }
  if (甲方签字 === '否') return '不适用';
  return '不确定';
}

function derive双方(甲方, 乙方) {
  if (甲方 === '是' && 乙方 === '是') return '是';
  if (甲方 === '否' || 乙方 === '否') return '否';
  return '不确定';
}

const pageResults = [];
for (let i = 0; i < doubaoItems.length; i += 1) {
  const prep = prepItems[i]?.json || {};
  const rawText = extractText(doubaoItems[i].json);
  let parsed = {
    甲方盖章: '不确定',
    乙方盖章: '不确定',
    甲方签字: '不确定',
    甲方身份证复印件: '不确定',
    说明: '',
  };
  const jsonData = parseStampJson(rawText);
  if (jsonData) {
    parsed = {
      甲方盖章: normalizeYesNo(jsonData['甲方盖章']),
      乙方盖章: normalizeYesNo(jsonData['乙方盖章']),
      甲方签字: normalizeYesNo(jsonData['甲方签字']),
      甲方身份证复印件: normalizeYesNo(jsonData['甲方身份证复印件']),
      说明: String(jsonData['说明'] || ''),
    };
  }
  pageResults.push({
    page: prep.stamp_page ?? base.stamp_pages[i] ?? i + 1,
    ...parsed,
  });
}

function aggregateField(pages, field) {
  let hasYes = false;
  let hasUncertain = false;
  for (const page of pages) {
    const val = normalizeYesNo(page[field]);
    if (val === '是') hasYes = true;
    else if (val === '不确定') hasUncertain = true;
  }
  if (hasYes) return '是';
  if (hasUncertain) return '不确定';
  return '否';
}

const 甲方盖章 = aggregateField(pageResults, '甲方盖章');
const 乙方盖章 = aggregateField(pageResults, '乙方盖章');
const 双方盖章 = derive双方(甲方盖章, 乙方盖章);
const 甲方签字 = aggregateField(pageResults, '甲方签字');
const 甲方身份证复印件 = aggregateField(pageResults, '甲方身份证复印件');
const 签字合同状态 = evaluateSignContract(甲方签字, 甲方身份证复印件, 甲方盖章);

return {
  json: {
    ...base,
    双方盖章,
    甲方盖章,
    乙方盖章,
    甲方签字,
    甲方身份证复印件,
    签字合同状态,
    签章页识别明细: pageResults,
    盖章说明: pageResults.map((r) => \`第\${r.page}页\`).join('、') || '',
    stamp_size_error: false,
  },
};
`;

const CODE_AGG = `const items = $input.all().map((i) => i.json);
let fields = items.find((x) => x.branch === 'fields') || {};
let stamp = items.find((x) => x.branch === 'stamp') || {};

// Merge 后兜底：直接引用支路节点
try {
  if (!fields.branch) fields = $('组装回写A载荷').first()?.json || fields;
} catch (_) {}
try {
  if (!stamp.branch) {
    stamp = $('签章支路结果').first()?.json || $('签章支路跳过结果').first()?.json || stamp;
  }
} catch (_) {}

function normalizeYesNo(value) {
  const v = String(value || '').trim();
  if (v === '是' || v === '否' || v === '不确定') return v;
  return '不确定';
}

function extractComparableString(val) {
  if (val === undefined || val === null) return '';
  if (typeof val === 'object') {
    if (Array.isArray(val)) return val.map(extractComparableString).join(',');
    return String(val.text ?? val.value ?? val.name ?? val.label ?? '');
  }
  return String(val).trim();
}

const 甲方 = normalizeYesNo(stamp.甲方盖章);
const 乙方 = normalizeYesNo(stamp.乙方盖章);
const 双方 = normalizeYesNo(stamp.双方盖章) !== '不确定'
  ? normalizeYesNo(stamp.双方盖章)
  : (甲方 === '是' && 乙方 === '是' ? '是' : (甲方 === '否' || 乙方 === '否' ? '否' : '不确定'));
const 甲方签字 = normalizeYesNo(stamp.甲方签字);
const 甲方身份证复印件 = normalizeYesNo(stamp.甲方身份证复印件);
const 签字合同状态 = stamp.签字合同状态 || '不适用';

const d = {
  record_id: fields.record_id || stamp.record_id,
  execNo: fields.execNo || stamp.execNo,
  活动名称: fields.活动名称 || '',
  活动日期: fields.活动日期 || '',
  活动人数: fields.活动人数 || '',
  活动总费用: fields.活动总费用 || '',
  付款比例和方式: fields.付款比例和方式 || '',
  付款比例是否预警: fields.付款比例是否预警,
  字段识别说明: fields.字段识别说明 || '',
  目的地: fields.目的地 || [],
  dest_source: fields.dest_source || '',
  预付款比例校验: fields.预付款比例校验 || '失败',
  盖章说明: stamp.盖章说明 || '',
  签章页识别明细: stamp.签章页识别明细 || [],
  stamp_size_error: Boolean(stamp.stamp_size_error),
};

const pageDetails = Array.isArray(d.签章页识别明细) ? d.签章页识别明细 : [];
const stampDetailText = pageDetails.length
  ? pageDetails
      .map(
        (r) =>
          \`第\${r.page}页：甲方盖章\${normalizeYesNo(r.甲方盖章)}，乙方盖章\${normalizeYesNo(r.乙方盖章)}，甲方签字\${normalizeYesNo(r.甲方签字)}，身份证\${normalizeYesNo(r.甲方身份证复印件)}\${r.说明 ? \`（\${r.说明}）\` : ''}\`
      )
      .join('；')
  : d.盖章说明 || '';

const lines = [
  '【合同校验】',
  d.execNo ? \`执行编号：\${d.execNo}\` : '',
  \`双方盖章：\${双方}\`,
  \`甲方盖章：\${甲方}\`,
  \`乙方盖章：\${乙方}\`,
  \`甲方签字：\${甲方签字}\`,
  \`甲方身份证复印件：\${甲方身份证复印件}\`,
  签字合同状态 && 签字合同状态 !== '不适用' ? \`签字合同状态：\${签字合同状态}\` : '',
  '签章判定依据：综合全部签章页聚合判断',
  \`活动名称：\${d.活动名称 || '-'}\`,
  \`活动日期：\${d.活动日期 || '-'}\`,
  \`活动人数：\${d.活动人数 || '-'}\`,
  \`活动总费用：\${d.活动总费用 || '-'}\`,
  \`付款比例和方式：\${d.付款比例和方式 || '-'}\`,
  \`预付款比例校验：\${d.预付款比例校验 || '-'}\`,
];

if (Array.isArray(d.目的地) && d.目的地.length) {
  lines.push(\`目的地：\${d.目的地.join('、')}（\${d.dest_source || '识别'}）\`);
} else {
  lines.push('目的地：未识别（不回写）');
}

if (d.字段识别说明) lines.push(\`字段识别说明：\${d.字段识别说明}\`);
if (stampDetailText) lines.push(\`盖章识别说明：\${stampDetailText}\`);

const missing = [
  !d.活动名称 ? '活动名称' : '',
  !d.活动日期 ? '活动日期' : '',
  !d.活动人数 ? '活动人数' : '',
  !d.活动总费用 ? '活动总费用' : '',
  !d.付款比例和方式 ? '付款比例和方式' : '',
].filter(Boolean);

const execCtx = $('02 检查订单合同').item?.json || {};
const refExecPeople = extractComparableString(execCtx.订单执行人数 || execCtx.执行人数 || '');
const refExecDate = extractComparableString(execCtx.订单执行日期 || execCtx.执行日期 || '');
const refContractPrice = extractComparableString(execCtx.订单合同价款 || '');
const crossIssues = [];
if (refExecPeople && d.活动人数 && refExecPeople !== String(d.活动人数)) {
  crossIssues.push(\`活动人数(\${d.活动人数})与执行人数(\${refExecPeople})不一致\`);
}
if (refExecDate && d.活动日期 && refExecDate !== String(d.活动日期)) {
  crossIssues.push(\`活动日期(\${d.活动日期})与执行日期(\${refExecDate})不一致\`);
}
if (refContractPrice && d.活动总费用 && refContractPrice !== String(d.活动总费用)) {
  crossIssues.push(\`活动总费用(\${d.活动总费用})与合同价款(\${refContractPrice})不一致\`);
}
if (crossIssues.length) lines.push(\`交叉核验：\${crossIssues.join('；')}\`);

const isBothStamped = 双方 === '是';
const hasSign = 甲方签字 === '是';
const isValidSignContract = 签字合同状态 === '有效';
const isInvalidSignContract = 签字合同状态 === '无效签字合同';
const sizeErr = d.stamp_size_error || (d.盖章说明 && (d.盖章说明.includes('超过10MB') || d.盖章说明.includes('超过64MB')));

let recognizeStatus = '校验正确';
if (sizeErr) {
  recognizeStatus = '文件过大';
} else if (isBothStamped) {
  recognizeStatus = missing.length > 0 ? '签章正确⚠️内容识别失败' : '校验正确';
} else if (missing.length >= 3 && !fields.fields_ok) {
  recognizeStatus = '识别失败';
} else if (甲方 === '否' && 乙方 === '否') {
  recognizeStatus = '双方未盖章';
} else if (甲方 === '否') {
  recognizeStatus = '甲方未盖章';
} else if (乙方 === '否') {
  recognizeStatus = '乙方未盖章';
} else if (双方 === '否') {
  recognizeStatus = '双方未盖章';
} else if (hasSign) {
  if (isInvalidSignContract) recognizeStatus = '无效签字合同';
  else if (isValidSignContract) recognizeStatus = '校验正确';
  else recognizeStatus = '需人工复核';
} else if (isValidSignContract) {
  recognizeStatus = '校验正确';
} else if (
  missing.length > 0 ||
  甲方 === '不确定' ||
  乙方 === '不确定' ||
  双方 === '不确定' ||
  签字合同状态 === '不确定'
) {
  recognizeStatus = '需人工复核';
}

const verifyInfo = lines.filter(Boolean).join('\\n');

return {
  json: {
    ...d,
    recognizeStatus,
    合同识别状态: recognizeStatus,
    校验信息: verifyInfo,
    feishu_ready: true,
    feishu_update_body: {
      fields: {
        合同识别状态: recognizeStatus,
        校验信息: verifyInfo,
      },
    },
  },
};
`;

function nodeByName(nodes, name) {
  const n = nodes.find((x) => x.name === name);
  if (!n) throw new Error('missing node: ' + name);
  return n;
}

function codeNode(name, jsCode, position, id) {
  return {
    parameters: { jsCode },
    type: 'n8n-nodes-base.code',
    typeVersion: 2,
    position,
    id: id || randomUUID(),
    name,
  };
}

function mainConn(...targets) {
  return { main: [targets.map((node) => ({ node, type: 'main', index: 0 }))] };
}

function build() {
  if (!fs.existsSync(BASE_PATH)) {
    throw new Error('Base workflow missing: ' + BASE_PATH);
  }
  const base = JSON.parse(fs.readFileSync(BASE_PATH, 'utf8'));
  const nodes = base.nodes.map((n) => JSON.parse(JSON.stringify(n)));
  const connections = {};

  // --- mutate existing ---
  nodeByName(nodes, '02 检查订单合同').parameters.jsCode = CODE_02;
  nodeByName(nodes, '准备签章图片').parameters.jsCode = CODE_PREP_STAMP;
  nodeByName(nodes, '解析盖章结果').parameters.jsCode = CODE_PARSE_STAMP;
  nodeByName(nodes, '解析盖章结果').parameters.mode = 'runOnceForAllItems';
  nodeByName(nodes, '04 聚合输出').parameters.jsCode = CODE_AGG;
  nodeByName(nodes, '04 聚合输出').parameters.mode = 'runOnceForAllItems';

  // Switch订单合同: only 常规 / 无合同
  const sw = nodeByName(nodes, 'Switch订单合同');
  sw.parameters.rules.values = [
    {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 3 },
        conditions: [
          {
            leftValue: '={{ $json.hasOrderContract }}',
            rightValue: '',
            operator: { type: 'boolean', operation: 'true', singleValue: true },
            id: '602c8bb8-6fa7-4fbb-b8f2-7ccdb31063ef',
          },
        ],
        combinator: 'and',
      },
      renameOutput: true,
      outputKey: '常规',
    },
    {
      conditions: {
        options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 3 },
        conditions: [
          {
            leftValue: '={{ $json.hasOrderContract }}',
            rightValue: '',
            operator: { type: 'boolean', operation: 'false', singleValue: true },
            id: '0acfacab-fa3c-4065-8711-8a1c96aa9d9b',
          },
        ],
        combinator: 'and',
      },
      renameOutput: true,
      outputKey: '无合同',
    },
  ];

  // Doubao: use prebuilt body + continue on fail
  const doubao = nodeByName(nodes, '豆包多模态_盖章识别');
  doubao.parameters.jsonBody = '={{ $json.doubao_request_body }}';
  doubao.continueOnFail = true;
  doubao.onError = 'continueRegularOutput';

  const agent = nodeByName(nodes, 'DeepSeek Agent_合同字段识别');
  agent.continueOnFail = true;
  agent.onError = 'continueRegularOutput';

  // rename final writeback
  const writeB = nodeByName(nodes, '回写识别结果');
  writeB.name = '回写B_状态与校验信息';

  // remove 标记文件过大
  const removeNames = new Set(['标记文件过大']);
  const filtered = nodes.filter((n) => !removeNames.has(n.name));

  // --- new nodes ---
  const destKeyword = codeNode('目的地关键词匹配', CODE_DEST_KEYWORD, [3760, -200]);
  const switchDest = {
    parameters: {
      rules: {
        values: [
          {
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 3 },
              conditions: [
                {
                  leftValue: '={{ $json.need_dest_llm }}',
                  rightValue: '',
                  operator: { type: 'boolean', operation: 'true', singleValue: true },
                  id: randomUUID(),
                },
              ],
              combinator: 'and',
            },
            renameOutput: true,
            outputKey: '需LLM',
          },
        ],
      },
      options: { fallbackOutput: 'extra' },
    },
    type: 'n8n-nodes-base.switch',
    typeVersion: 3.4,
    position: [4000, -200],
    id: randomUUID(),
    name: 'Switch目的地',
  };
  // fallbackOutput extra = 关键词已命中，直接组装回写A

  const destAgent = {
    parameters: {
      promptType: 'define',
      text: `={{ '活动名称：' + ($json.活动名称 || '') + '\\n请从下列目的地选项中选择匹配项（可多选），只返回 JSON：{\\\"目的地\\\":[]}。选项：' + ($json.DEST_OPTIONS || []).join('、') }}`,
      options: {
        systemMessage:
          '你是目的地识别助手。只能从给定选项中选择，不要发明新地名。若无法判断返回 {"目的地":[]}。不要 markdown。',
      },
    },
    type: '@n8n/n8n-nodes-langchain.agent',
    typeVersion: 1.7,
    position: [4240, -320],
    id: randomUUID(),
    name: 'DeepSeek Agent_目的地兜底',
    continueOnFail: true,
    onError: 'continueRegularOutput',
  };

  const destMergeLlm = codeNode('合并目的地LLM结果', CODE_DEST_MERGE_LLM, [4480, -320]);
  const buildWriteA = codeNode('组装回写A载荷', CODE_BUILD_WRITE_A, [4720, -200]);

  const writeA = {
    parameters: {
      method: 'PUT',
      url: "=https://open.feishu.cn/open-apis/bitable/v1/apps/{{ $env.FEISHU_BASE_ID }}/tables/tbl821KbTBpjjyXI/records/{{ $json.record_id }}",
      sendHeaders: true,
      headerParameters: {
        parameters: [
          {
            name: 'Authorization',
            value: '=Bearer {{ $node["获取飞书Token"].json["tenant_access_token"] }}',
          },
          { name: 'Content-Type', value: 'application/json' },
        ],
      },
      sendBody: true,
      specifyBody: 'json',
      jsonBody: '={{ JSON.stringify($json.feishu_update_body_a) }}',
      options: {},
    },
    type: 'n8n-nodes-base.httpRequest',
    typeVersion: 4.2,
    position: [4960, -200],
    id: randomUUID(),
    name: '回写A_预付款与目的地',
    continueOnFail: true,
    onError: 'continueRegularOutput',
  };

  const switchStamp = {
    parameters: {
      rules: {
        values: [
          {
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 3 },
              conditions: [
                {
                  leftValue: '={{ $json.skip_doubao }}',
                  rightValue: '',
                  operator: { type: 'boolean', operation: 'true', singleValue: true },
                  id: randomUUID(),
                },
              ],
              combinator: 'and',
            },
            renameOutput: true,
            outputKey: '跳过豆包',
          },
        ],
      },
      options: { fallbackOutput: 'extra' },
    },
    type: 'n8n-nodes-base.switch',
    typeVersion: 3.4,
    position: [4048, 80],
    id: randomUUID(),
    name: 'Switch签章',
  };

  const writeSize = {
    parameters: {
      method: 'PUT',
      url: "=https://open.feishu.cn/open-apis/bitable/v1/apps/{{ $env.FEISHU_BASE_ID }}/tables/tbl821KbTBpjjyXI/records/{{ $json.record_id }}",
      sendHeaders: true,
      headerParameters: {
        parameters: [
          {
            name: 'Authorization',
            value: '=Bearer {{ $node["获取飞书Token"].json["tenant_access_token"] }}',
          },
          { name: 'Content-Type', value: 'application/json' },
        ],
      },
      sendBody: true,
      specifyBody: 'json',
      jsonBody:
        '={{ JSON.stringify({ fields: { 合同识别状态: "文件过大", 校验信息: $json.skip_doubao_reason || "签章图片或请求体超过豆包限制" } }) }}',
      options: {},
    },
    type: 'n8n-nodes-base.httpRequest',
    typeVersion: 4.2,
    position: [4288, 200],
    id: randomUUID(),
    name: '回写_签章超限',
    continueOnFail: true,
    onError: 'continueRegularOutput',
  };

  const stampSkip = codeNode('签章支路跳过结果', CODE_STAMP_SKIP, [4528, 200]);
  const stampPass = codeNode('签章支路结果', CODE_STAMP_PASS, [4720, 80]);

  const merge = {
    parameters: {
      mode: 'combine',
      combineBy: 'combineByPosition',
      options: {},
    },
    type: 'n8n-nodes-base.merge',
    typeVersion: 3.2,
    position: [5200, -64],
    id: randomUUID(),
    name: 'Merge双支路',
  };

  // reposition parallel nodes
  nodeByName(filtered, '准备合同文本').position = [3120, -200];
  nodeByName(filtered, 'DeepSeek Agent_合同字段识别').position = [3360, -200];
  nodeByName(filtered, 'DeepSeek Chat Model').position = [3360, -40];
  nodeByName(filtered, '解析DeepSeek输出').position = [3600, -200];
  nodeByName(filtered, '准备签章图片').position = [3360, 80];
  nodeByName(filtered, '豆包多模态_盖章识别').position = [4288, 80];
  nodeByName(filtered, '解析盖章结果').position = [4528, 80];
  nodeByName(filtered, '04 聚合输出').position = [5440, -64];
  writeB.position = [5680, -64];

  const allNodes = [
    ...filtered,
    destKeyword,
    switchDest,
    destAgent,
    destMergeLlm,
    buildWriteA,
    writeA,
    switchStamp,
    writeSize,
    stampSkip,
    stampPass,
    merge,
  ];

  // --- connections (rebuild fully for clarity) ---
  const C = connections;
  C['Webhook'] = mainConn('01 检查执行记录');
  C['01 检查执行记录'] = mainConn('获取飞书Token');
  C['获取飞书Token'] = mainConn('Switch执行记录');
  C['Switch执行记录'] = {
    main: [
      [{ node: '获取关联订单', type: 'main', index: 0 }],
      [{ node: '标记无合同', type: 'main', index: 0 }],
    ],
  };
  C['获取关联订单'] = mainConn('02 检查订单合同');
  C['02 检查订单合同'] = mainConn('Switch订单合同');
  C['Switch订单合同'] = {
    main: [
      [{ node: '标记识别中', type: 'main', index: 0 }],
      [{ node: '标记无合同', type: 'main', index: 0 }],
    ],
  };
  C['标记识别中'] = mainConn('获取临时下载链接');
  C['获取临时下载链接'] = mainConn('下载PDF');
  C['下载PDF'] = mainConn('调用合同解析服务');

  // parallel fork
  C['调用合同解析服务'] = {
    main: [
      [
        { node: '准备合同文本', type: 'main', index: 0 },
        { node: '准备签章图片', type: 'main', index: 0 },
      ],
    ],
  };

  // fields branch
  C['准备合同文本'] = mainConn('DeepSeek Agent_合同字段识别');
  C['DeepSeek Chat Model'] = {
    ai_languageModel: [[{ node: 'DeepSeek Agent_合同字段识别', type: 'ai_languageModel', index: 0 }]],
  };
  // also wire model to dest agent
  C['DeepSeek Chat Model'].ai_languageModel[0].push({
    node: 'DeepSeek Agent_目的地兜底',
    type: 'ai_languageModel',
    index: 0,
  });

  C['DeepSeek Agent_合同字段识别'] = mainConn('解析DeepSeek输出');
  C['解析DeepSeek输出'] = mainConn('目的地关键词匹配');
  C['目的地关键词匹配'] = mainConn('Switch目的地');
  C['Switch目的地'] = {
    main: [
      [{ node: 'DeepSeek Agent_目的地兜底', type: 'main', index: 0 }],
      [{ node: '组装回写A载荷', type: 'main', index: 0 }],
    ],
  };
  C['DeepSeek Agent_目的地兜底'] = mainConn('合并目的地LLM结果');
  C['合并目的地LLM结果'] = mainConn('组装回写A载荷');
  C['组装回写A载荷'] = mainConn('回写A_预付款与目的地');
  C['回写A_预付款与目的地'] = {
    main: [[{ node: 'Merge双支路', type: 'main', index: 0 }]],
  };

  // stamp branch
  C['准备签章图片'] = mainConn('Switch签章');
  C['Switch签章'] = {
    main: [
      [{ node: '回写_签章超限', type: 'main', index: 0 }],
      [{ node: '豆包多模态_盖章识别', type: 'main', index: 0 }],
    ],
  };
  // 跳过豆包 includes size error AND no image — size error also hits 回写_签章超限
  // Refine: only stamp_size_error should write 文件过大 early; no-image skips writeSize
  // Reconnect via IF on stamp_size_error inside skip path:
  C['回写_签章超限'] = mainConn('签章支路跳过结果');
  C['签章支路跳过结果'] = {
    main: [[{ node: 'Merge双支路', type: 'main', index: 1 }]],
  };
  C['豆包多模态_盖章识别'] = mainConn('解析盖章结果');
  C['解析盖章结果'] = mainConn('签章支路结果');
  C['签章支路结果'] = {
    main: [[{ node: 'Merge双支路', type: 'main', index: 1 }]],
  };

  C['Merge双支路'] = mainConn('04 聚合输出');
  C['04 聚合输出'] = mainConn('回写B_状态与校验信息');

  // Fix Switch签章: skip_doubao true -> need split size vs no image
  // Replace with: skip -> Code that optionally writes size, always 签章支路跳过结果
  // Simpler fix: change 回写_签章超限 to only run when stamp_size_error; use another switch

  // Rebuild stamp switch to 3 outputs via nested logic in one Code before switch
  // For now: 回写_签章超限 always on skip — for 未获取签章页图片 writing 文件过大 is WRONG.
  // Patch writeSize jsonBody to only set 文件过大 when stamp_size_error, else no-op pass-through...
  // Better: insert Switch超限 between skip and writeSize.

  const switchSize = {
    parameters: {
      rules: {
        values: [
          {
            conditions: {
              options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 3 },
              conditions: [
                {
                  leftValue: '={{ $json.stamp_size_error }}',
                  rightValue: '',
                  operator: { type: 'boolean', operation: 'true', singleValue: true },
                  id: randomUUID(),
                },
              ],
              combinator: 'and',
            },
            renameOutput: true,
            outputKey: '超限',
          },
        ],
      },
      options: { fallbackOutput: 'extra' },
    },
    type: 'n8n-nodes-base.switch',
    typeVersion: 3.4,
    position: [4160, 200],
    id: randomUUID(),
    name: 'Switch超限',
  };
  allNodes.push(switchSize);

  C['Switch签章'] = {
    main: [
      [{ node: 'Switch超限', type: 'main', index: 0 }],
      [{ node: '豆包多模态_盖章识别', type: 'main', index: 0 }],
    ],
  };
  C['Switch超限'] = {
    main: [
      [{ node: '回写_签章超限', type: 'main', index: 0 }],
      [{ node: '签章支路跳过结果', type: 'main', index: 0 }],
    ],
  };

  const workflow = {
    name: '合同校验重新识别（升级）',
    nodes: allNodes,
    connections: C,
    settings: base.settings || { executionOrder: 'v1' },
    pinData: {},
    meta: Object.assign({}, base.meta || {}, {
      templateCredsSetupCompleted: true,
      builtBy: 'scripts/contract_reid_upgrade_workflow.js',
      builtAt: new Date().toISOString(),
    }),
  };

  fs.mkdirSync(path.dirname(OUT_PATH), { recursive: true });
  fs.writeFileSync(OUT_PATH, JSON.stringify(workflow, null, 2), 'utf8');
  return workflow;
}

function selfTest() {
  const a = matchDestinationsByKeyword('安吉两日团建');
  if (!a.includes('安吉')) throw new Error('keyword 安吉 failed: ' + a);
  const b = matchDestinationsByKeyword('杭州西湖一日游');
  if (!b.includes('杭州内')) throw new Error('alias 杭州 failed: ' + b);
  const c = matchDestinationsByKeyword('未知星球活动');
  if (c.length) throw new Error('expected empty, got ' + c);
  console.log('selfTest OK');
}

if (require.main === module) {
  selfTest();
  const wf = build();
  console.log('Wrote', OUT_PATH);
  console.log('nodes', wf.nodes.length);
  console.log('Import this file in n8n UI. Deactivate old workflow first (same webhook path).');
}

module.exports = { matchDestinationsByKeyword, build, DEST_OPTIONS };
