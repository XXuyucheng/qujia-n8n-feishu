/**
 * 判定生成方式（读取订单记录 → 判定生成方式）
 *
 * 生成方式读自「报价生成状态」：附件生成 / 按需生成
 * 按需校验只看「客户需求」文案解析结果（不读订单表结构化字段）
 * 附件：不校验
 *
 * 错误码：缺生成方式 | 缺人数 | 缺出发地 | 缺目的地 | 缺出发日期 | 缺活动天数
 *
 * 同步：node scripts/sync_detect_quote_generate_mode.js
 */

function selectFieldText(field) {
  if (field == null) return '';
  if (typeof field === 'string') return field.trim();
  if (typeof field === 'number') return String(field);
  if (Array.isArray(field)) {
    return field
      .map((x) => selectFieldText(x))
      .filter(Boolean)
      .join('');
  }
  if (typeof field === 'object') {
    if (typeof field.text === 'string' && field.text.trim()) return field.text.trim();
    if (typeof field.name === 'string' && field.name.trim()) return field.name.trim();
    if (Array.isArray(field.value) && field.value.length) {
      return selectFieldText(field.value);
    }
  }
  return '';
}

/** 从客户需求文案取「标签：值」（行首标签；值可为空） */
function pickFromRequirement(text, labels) {
  const lines = String(text || '').split(/\r?\n/);
  for (const label of labels) {
    const re = new RegExp('^\\s*' + label + '\\s*[：:]\\s*(.*)$');
    for (const line of lines) {
      const m = line.match(re);
      if (m) return String(m[1] || '').trim();
    }
  }
  return '';
}

function parsePeople(raw) {
  if (raw == null || raw === '') return NaN;
  const m = String(raw).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/);
  return m ? Number(m[1]) : NaN;
}

const recordId = $('解析触发记录').first().json.record_id;
const token = $('获取飞书Token').first().json.tenant_access_token || '';
const resp = $input.first().json;
const data = resp.data || resp;
const record = data.record || data;
const fields = record.fields || {};

// —— 生成方式 =「报价生成状态」——
const modeText = selectFieldText(fields['报价生成状态']);
let generate_mode = '';
if (modeText.includes('按需')) generate_mode = 'ondemand';
else if (modeText.includes('附件')) generate_mode = 'attachment';

const 订单号 =
  selectFieldText(fields['订单号']) ||
  selectFieldText(fields['订单编号']) ||
  recordId;
const 客户需求 = selectFieldText(fields['客户需求']);

// —— 仅从客户需求解析业务字段 ——
const 出发地 = pickFromRequirement(客户需求, ['出发地']);
const 目的地 = pickFromRequirement(客户需求, ['目的地']);
const 活动日期 = pickFromRequirement(客户需求, [
  '活动日期',
  '出发日期',
  '执行日期',
]);
const 活动天数 = pickFromRequirement(客户需求, ['活动天数', '天数']);
const 报价人数Raw = pickFromRequirement(客户需求, [
  '人数',
  '报价人数',
  '执行人数',
]);
const 报价人数Num = parsePeople(报价人数Raw);
const 报价人数 =
  Number.isFinite(报价人数Num) && 报价人数Num > 0 ? 报价人数Num : 报价人数Raw;

const errorCodes = [];
if (!generate_mode) errorCodes.push('缺生成方式');

if (generate_mode === 'ondemand') {
  if (!Number.isFinite(报价人数Num) || 报价人数Num <= 0) errorCodes.push('缺人数');
  if (!出发地) errorCodes.push('缺出发地');
  if (!目的地) errorCodes.push('缺目的地');
  if (!活动日期) errorCodes.push('缺出发日期');
  if (!活动天数) errorCodes.push('缺活动天数');
}

const validation_ok = errorCodes.length === 0;
const error_code = validation_ok ? '' : errorCodes[0];

return [
  {
    json: {
      validation_ok,
      error_code,
      error_codes: errorCodes,

      record_id: record.record_id || recordId,
      tenant_access_token: token,

      订单号,
      generate_mode,
      is_attachment: generate_mode === 'attachment',
      is_ondemand: generate_mode === 'ondemand',
      mode_text: modeText,
      客户需求,
      活动名称: selectFieldText(fields['活动名称']),
      报价人数,
      活动天数,
      活动日期,
      出发地,
      目的地,
      出发地目的地: [出发地, 目的地].filter(Boolean).join('-'),
      fields,
    },
  },
];
