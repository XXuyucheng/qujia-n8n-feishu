/**
 * 报价附件表格识别（Extract from File → 报价信息提取）
 *
 * 约定：
 * 1. 用「键最多且 ≥6」的行确定源列 key 顺序（最多 7 列 A–G），再映射全部行
 *    —— 避免用表头稀疏行（缺「趣加团建」类目列）定序导致总价行对不上
 * 2. 动态识别明细表头并映射列角色（含「网络价格」等中间列）
 * 3. 任一字格含「活动总价」，且同行其它格恰有一个数字 → 明细结束
 * 4. 表头上方用 A 标签 + 其后首个非空列取值提取活动名称/人数/天数/日期
 *
 * 同步到 n8n：复制本文件全文到「报价信息提取」Code 节点
 * （或改完后跑：node scripts/sync_quote_attachment_extract.js）
 */

// 上游：Extract from File 的全部行
const rawRows = $input.all().map((i) => i.json);

const MAX_COLS = 7;
const COLS = ['A', 'B', 'C', 'D', 'E', 'F', 'G'].slice(0, MAX_COLS);

/** 读单元格；空 → '' */
function cell(row, key) {
  const v = row?.[key];
  if (v == null || v === '') return '';
  return String(v).trim();
}

/** 转数字；非法 → null */
function num(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  const s = String(v).trim().replace(/,/g, '');
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** 只保留中文（类目清洗：去掉编号、英文、符号） */
function stripToChinese(raw) {
  return String(raw || '')
    .replace(/[^\u4e00-\u9fff]/g, '')
    .trim();
}

/** 税费/服务费类目：不进明细，抽百分比 */
function isTaxCategory(cat) {
  return /税费|服务费/.test(String(cat || ''));
}

/**
 * 解析百分比 → { display, ratio } | null
 */
function parsePercent(raw) {
  if (raw == null || raw === '') return null;

  if (typeof raw === 'string') {
    const s = raw.trim().replace(/％/g, '%');
    const m = s.match(/^(\d+(?:\.\d+)?)\s*%$/);
    if (m) {
      const pct = Number(m[1]);
      return { display: `${pct}%`, ratio: pct / 100 };
    }
    const n = Number(s);
    if (!Number.isFinite(n)) return null;
    raw = n;
  }

  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) return null;
  if (raw > 0 && raw <= 1) {
    const pct = Math.round(raw * 10000) / 100;
    return { display: `${pct}%`, ratio: raw };
  }
  if (raw <= 100) {
    return { display: `${raw}%`, ratio: raw / 100 };
  }
  return null;
}

/** 从 A–G 找第一个百分比 */
function extractPercentFromRow(row) {
  for (const k of COLS) {
    const parsed = parsePercent(row?.[k]);
    if (parsed) return parsed;
  }
  return null;
}

/** Excel 序列号 / 文本 → 中文日期 */
function fmtCnDate(raw) {
  if (raw == null || raw === '') return '';

  const n = typeof raw === 'number' ? raw : Number(String(raw).trim());
  if (Number.isFinite(n) && n > 20000 && n < 80000 && !/[\/\-年]/.test(String(raw))) {
    const d = new Date(Date.UTC(1899, 11, 30) + Math.floor(n) * 86400000);
    return `${d.getUTCFullYear()}年${d.getUTCMonth() + 1}月${d.getUTCDate()}日`;
  }

  const s = String(raw).trim();
  if (/年/.test(s)) return s;
  const m = s.match(/(\d{4})[\/\-年](\d{1,2})[\/\-月]?(\d{1,2})?/);
  if (m) return `${m[1]}年${Number(m[2])}月${Number(m[3] || 1)}日`;
  return s;
}

/**
 * 选「键最多」的行定 A–G 源键（至少 6 个）。
 * 表头行常缺类目列（趣加团建），满键明细行才含完整物理列序。
 */
function detectSourceKeys(inputRows) {
  let best = null;
  let bestLen = 0;
  for (const row of inputRows) {
    if (!row || typeof row !== 'object') continue;
    const keys = Object.keys(row);
    if (keys.length >= 6 && keys.length > bestLen) {
      best = keys;
      bestLen = keys.length;
    }
  }
  if (!best) {
    throw new Error(
      '未找到含完整 6 个键的行，无法确定列顺序（请确认 Extract from File 输出）',
    );
  }
  return best.slice(0, MAX_COLS);
}

/**
 * 将 Extract 行归一化为 { A,B,C,... }
 * sourceKeys：全表统一的原始字段名
 */
function normalizeRows(inputRows, sourceKeys) {
  return inputRows.map((row) => {
    const out = {};
    for (const k of COLS) out[k] = '';
    const src = row && typeof row === 'object' ? row : {};
    for (let i = 0; i < sourceKeys.length && i < COLS.length; i++) {
      const k = sourceKeys[i];
      out[COLS[i]] = k != null && k in src ? src[k] : '';
    }
    return out;
  });
}

function rowTexts(row) {
  return COLS.map((k) => cell(row, k));
}

function rowBlob(row) {
  return rowTexts(row).filter(Boolean).join(' ');
}

/**
 * 明细表头：同一行需同时出现 物品名称、描述、数量、单价
 * 返回 { index, colMap }
 */
function detectDetailHeader(rows) {
  const idx = rows.findIndex((r) => {
    const blob = rowBlob(r);
    return (
      blob.includes('物品名称') &&
      blob.includes('描述') &&
      blob.includes('数量') &&
      blob.includes('单价')
    );
  });
  if (idx < 0) {
    throw new Error('未找到明细表头行（需含：物品名称、描述、数量、单价）');
  }

  const header = rows[idx];
  const colMap = {
    categoryCol: null,
    nameCol: null,
    descCol: null,
    qtyCol: null,
    priceCol: null,
    totalCol: null,
  };

  for (const k of COLS) {
    const t = cell(header, k);
    if (!t) continue;
    if (t.includes('类目') || t.includes('分类')) colMap.categoryCol = k;
    if (t.includes('物品名称') || t === '名称') colMap.nameCol = k;
    if (t.includes('描述') || t.includes('内容')) colMap.descCol = k;
    if (t.includes('数量')) colMap.qtyCol = k;
    if (t.includes('单价')) colMap.priceCol = k;
    if (t.includes('总价') || t.includes('小计') || t.includes('金额')) colMap.totalCol = k;
  }

  // 表头 A 为空 → 默认 A 为类目列
  if (!colMap.categoryCol && !cell(header, 'A')) {
    colMap.categoryCol = 'A';
  }
  if (!colMap.categoryCol) colMap.categoryCol = 'A';
  if (!colMap.nameCol) colMap.nameCol = 'B';
  if (!colMap.descCol) colMap.descCol = 'C';
  if (!colMap.qtyCol) colMap.qtyCol = 'D';
  if (!colMap.priceCol) colMap.priceCol = 'E';
  if (!colMap.totalCol) colMap.totalCol = 'F';

  return { index: idx, colMap };
}

/**
 * 活动总价行：任一字格文本含「活动总价」，且其余格恰有一个可解析数字
 * （兼容总价写在数量/单价列，如 __EMPTY_2=79800）
 */
function findActivityTotalIndex(rows, afterIdx) {
  for (let i = afterIdx + 1; i < rows.length; i++) {
    const r = rows[i];
    let labelCol = null;
    for (const k of COLS) {
      if (cell(r, k).includes('活动总价')) {
        labelCol = k;
        break;
      }
    }
    if (!labelCol) continue;

    let numericCount = 0;
    for (const k of COLS) {
      if (k === labelCol) continue;
      const raw = r[k];
      if (raw == null || raw === '') continue;
      if (num(raw) != null) numericCount += 1;
    }
    if (numericCount === 1) return i;
  }
  return -1;
}

/** 标签行取值：A 为标签时，取 B→G 第一个非空 */
function firstValueAfterA(row) {
  for (const k of COLS) {
    if (k === 'A') continue;
    const raw = row[k];
    if (raw != null && raw !== '') return raw;
  }
  return '';
}

/**
 * 表头上方元数据：A 标签，其后首个非空列为值
 */
function extractMetaAbove(rows, headerIdx) {
  let 活动名称 = '';
  let 报价人数 = '';
  let 活动天数 = '';
  let 活动日期 = '';

  for (let i = 0; i < headerIdx; i++) {
    const r = rows[i];
    const a = cell(r, 'A');
    if (!a) continue;

    const label = a.replace(/\s*[:：]\s*$/, '').trim();
    const valueRaw = firstValueAfterA(r);
    const valueText =
      valueRaw == null || valueRaw === '' ? '' : String(valueRaw).trim();

    if (label === '活动日期' && !活动日期) {
      活动日期 = fmtCnDate(valueRaw);
      continue;
    }

    if (label.includes('人数') && 报价人数 === '') {
      const n = num(valueRaw);
      报价人数 = n != null ? n : valueText;
      continue;
    }

    if ((label === '活动天数' || label.includes('活动天数')) && !活动天数) {
      活动天数 = valueText;
      continue;
    }

    if (!活动名称) {
      if (label === '活动名称' || /名称$/.test(label)) {
        活动名称 = valueText;
      }
    }
  }

  return { 活动名称, 报价人数, 活动天数, 活动日期 };
}

// —— 主流程 ——
const sourceKeys = detectSourceKeys(rawRows);
const rows = normalizeRows(rawRows, sourceKeys);
const { index: headerIdx, colMap } = detectDetailHeader(rows);

const totalIdx = findActivityTotalIndex(rows, headerIdx);
if (totalIdx < 0) {
  throw new Error(
    '未找到「活动总价」行（要求任一字格含活动总价，且同行其余格恰有一个数字）',
  );
}

const { 活动名称, 报价人数, 活动天数, 活动日期 } = extractMetaAbove(rows, headerIdx);

const sheet_rows = [];
let lastCategory = '';
let lastItemName = '';
let 税费及服务 = '';
let taxRatio = null;

for (let i = headerIdx + 1; i < totalIdx; i++) {
  const r = rows[i];

  const catRaw = cell(r, colMap.categoryCol);
  const catZh = stripToChinese(catRaw);
  if (catZh) lastCategory = catZh;
  const 类目 = lastCategory || '费用';

  const name = cell(r, colMap.nameCol);
  const desc = cell(r, colMap.descCol)
    .replace(/[\r\n]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const qtyRaw = r[colMap.qtyCol];
  const priceRaw = r[colMap.priceCol];

  // 税费类：不进明细，抽百分比
  if (isTaxCategory(类目)) {
    if (!税费及服务) {
      const parsed =
        extractPercentFromRow(r) ||
        parsePercent(priceRaw) ||
        parsePercent(qtyRaw) ||
        parsePercent(desc) ||
        parsePercent(catRaw);
      if (parsed) {
        税费及服务 = parsed.display;
        taxRatio = parsed.ratio;
      }
    }
    continue;
  }

  // 跳过表头残留
  if (name === '物品名称' || desc === '描述') continue;

  const qtyN = num(qtyRaw);
  const priceN = num(priceRaw);
  // 不因总价为 0 过滤：有物品名称 / 描述 / 单价任一即可进明细

  if (name) lastItemName = name;
  const itemName = name || lastItemName || '';

  // 仅类目行或完全空行：不产出明细
  if (!itemName && !desc && priceN == null) continue;

  sheet_rows.push({
    类目,
    物品名称: itemName || '(未命名)',
    描述: desc,
    数量: qtyN,
    单价: priceN,
  });
}

const order = $('读取订单记录').first().json.data.record;
const orderRecordId = order.record_id || '';
const orderNo = order.fields?.订单号 || orderRecordId;

return [
  {
    json: {
      record_id: orderRecordId,
      活动名称,
      报价人数,
      活动天数,
      活动日期,
      出发地目的地: '', // 本算法不匹配该占位符
      税费及服务,
      税费及服务比例: taxRatio,
      订单号: orderNo,
      sheet_rows,
      报价明细条目: sheet_rows,
    },
  },
];
