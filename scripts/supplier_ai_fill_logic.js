/**
 * 供应商 AI 补全 — 纯函数逻辑（供单测与 n8n Code 节点对照）
 */

const TYPE_WHITELIST = new Set([
  '酒店',
  '民宿',
  '其它',
  '车队',
  '景区 (基地)',
  '景区（基地）',
  '餐厅',
  '客户退款',
  '地接',
  '导游',
  '内部',
  '广告搭建',
]);

const TYPE_NORMALIZE = {
  '景区（基地）': '景区 (基地)',
  '景区 (基地)': '景区 (基地)',
};

const QUAL_WHITELIST = new Set(['旅行社', '其他', '餐饮', '个人']);

const REGION_PREFERRED = new Set([
  '舟山',
  '安吉',
  '德清',
  '嘉兴',
  '金华',
  '苏州',
  '桐庐',
  '上海崇明区',
  '上海奉贤区',
  '上海青浦区',
  '千岛湖',
  '杭州',
  '湖州',
  '上海',
  '绍兴',
]);

function pickText(field) {
  if (field == null) return '';
  if (typeof field === 'string') return field.trim();
  if (typeof field === 'number') return String(field);
  if (Array.isArray(field)) {
    if (!field.length) return '';
    const first = field[0];
    if (typeof first === 'string') return first.trim();
    if (first && typeof first.text === 'string') return first.text.trim();
    return pickText(first);
  }
  if (typeof field === 'object') {
    if (Array.isArray(field.value) && field.value.length) {
      return pickText(field.value);
    }
    if (typeof field.text === 'string') return field.text.trim();
  }
  return '';
}

function isEmptyField(field) {
  return pickText(field) === '';
}

function normalizeType(value) {
  const v = String(value || '').trim();
  if (!v) return '';
  if (TYPE_NORMALIZE[v]) return TYPE_NORMALIZE[v];
  if (TYPE_WHITELIST.has(v)) return v;
  return '';
}

function normalizeQual(value) {
  const v = String(value || '').trim();
  if (!v) return '';
  return QUAL_WHITELIST.has(v) ? v : '';
}

function normalizeRegion(value) {
  const v = String(value || '').trim();
  return v;
}

/**
 * @param {object} existing - { 类型, 地域, 资质 } 原始或已 pickText 的值
 * @param {object} ai - { 类型, 地域, 资质, low_confidence?: string[] }
 * @returns {{ fields: object, aiMark: string|null, skipped: boolean, reasons: string[] }}
 */
function buildUpdateForRecord(existing, ai) {
  const curType = pickText(existing['类型']);
  const curRegion = pickText(existing['地域']);
  const curQual = pickText(existing['资质']);

  const fields = {};
  const reviewParts = [];
  const reasons = [];
  const low = new Set(
    Array.isArray(ai.low_confidence)
      ? ai.low_confidence.map((x) => String(x).trim()).filter(Boolean)
      : [],
  );

  if (!curType) {
    const t = normalizeType(ai['类型']);
    if (t) {
      fields['类型'] = t;
      if (low.has('类型')) reviewParts.push('类型');
    } else {
      reasons.push('类型无法写入');
      reviewParts.push('类型');
    }
  }

  if (!curRegion) {
    const r = normalizeRegion(ai['地域']);
    if (r) {
      fields['地域'] = r;
      if (low.has('地域') || !REGION_PREFERRED.has(r)) reviewParts.push('地域');
    } else {
      reasons.push('地域为空');
      reviewParts.push('地域');
    }
  }

  if (!curQual) {
    const q = normalizeQual(ai['资质']);
    if (q) {
      fields['资质'] = q;
      if (low.has('资质')) reviewParts.push('资质');
    } else {
      reasons.push('资质无法写入');
      reviewParts.push('资质');
    }
  }

  // 已有字段上的 low_confidence 忽略（不会覆盖）
  const uniqueReview = [...new Set(reviewParts)];
  const aiMark = uniqueReview.length ? `AI 待核：${uniqueReview.join('/')}` : null;

  if (aiMark) {
    fields['AI 标记'] = aiMark;
  }

  const hasBusinessWrite =
    fields['类型'] != null || fields['地域'] != null || fields['资质'] != null;
  const skipped = !hasBusinessWrite && !aiMark;

  return { fields, aiMark, skipped, reasons };
}

function parseAiBatch(raw) {
  const text = String(raw ?? '')
    .replace(/```json/gi, '')
    .replace(/```/g, '')
    .trim();
  const start = text.indexOf('[');
  const end = text.lastIndexOf(']');
  if (start < 0 || end <= start) {
    throw new Error('AI 输出不是 JSON 数组');
  }
  const data = JSON.parse(text.slice(start, end + 1));
  if (!Array.isArray(data)) {
    throw new Error('AI 输出根节点不是数组');
  }
  return data;
}

/**
 * 按数组下标对齐 AI 结果与原记录（record_id 只取自原 item，不用 AI 返回）
 * @param {Array<{record_id:string, fields:object}>} batchRecords
 * @param {Array<object>} aiItems
 */
function alignAndBuildUpdates(batchRecords, aiItems) {
  const updates = [];
  const errors = [];
  const list = Array.isArray(aiItems) ? aiItems : [];

  for (let i = 0; i < batchRecords.length; i += 1) {
    const rec = batchRecords[i];
    const id = rec.record_id;
    const ai = list[i];
    if (!ai || typeof ai !== 'object') {
      errors.push({ record_id: id, index: i, error: 'AI 未返回该下标结果' });
      continue;
    }
    const result = buildUpdateForRecord(rec.fields || {}, ai);
    if (result.skipped) {
      errors.push({
        record_id: id,
        index: i,
        error: '无可写字段',
        reasons: result.reasons,
      });
      continue;
    }
    updates.push({
      record_id: id,
      fields: result.fields,
      aiMark: result.aiMark,
      reasons: result.reasons,
    });
  }

  if (list.length !== batchRecords.length) {
    errors.push({
      error: `AI 条数 ${list.length} 与本批 ${batchRecords.length} 不一致`,
    });
  }

  return { updates, errors };
}

function needsFill(fields) {
  return (
    isEmptyField(fields['类型']) ||
    isEmptyField(fields['地域']) ||
    isEmptyField(fields['资质'])
  );
}

module.exports = {
  TYPE_WHITELIST,
  QUAL_WHITELIST,
  REGION_PREFERRED,
  pickText,
  isEmptyField,
  normalizeType,
  normalizeQual,
  normalizeRegion,
  buildUpdateForRecord,
  parseAiBatch,
  alignAndBuildUpdates,
  needsFill,
};
