/**
 * 出团计划单 — 预定信息生成
 *
 * 职责：
 * 1. 为每条报价明细生成可发给供应商的「预定信息」文案
 * 2. 预定时间按明细名称中的 DAY/day 序号相对「执行日期」推算：
 *    - DAY1 / day1 → 执行日期当天
 *    - DAY2 / day2 → 执行日期 + 1 天
 *    - 名称中无 dayN → 回退为执行日期本身
 * 3. 若上游只有「活动行程条目」，拼成「活动行程」正文供下游占位符使用
 *
 * 依赖节点：
 *   - $input ← 「在线报价信息提取」
 *   - $('查询待识别执行记录') ← 执行表字段（订单号、执行日期、策划）
 *   - $('解析在线报价链接') ← order_record_id（写入 _meta）
 *
 * 使用：粘贴到 n8n「预定信息生成」Code 节点；或与本文件同步维护后手动粘贴。
 */

const quote = $input.first().json;

// 本工作流暂无 Loop；取「查询待识别执行记录」第一条
const execResp = $('查询待识别执行记录').first().json;
const execItem = execResp?.data?.items?.[0] || {};
const execFields = execItem.fields || {};

/** 飞书多维表格字段 / 字符串清洗 */
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

/**
 * 解析为 { y, m, d }。
 * 支持：毫秒时间戳、飞书日期对象、`2026-08-30` / `2026年8月30日` 等文本。
 */
function toDateParts(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'number') {
    const d = new Date(v > 1e12 ? v : v);
    if (Number.isNaN(d.getTime())) return null;
    return { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate() };
  }
  if (typeof v === 'object' && Array.isArray(v.value) && typeof v.value[0] === 'number') {
    return toDateParts(v.value[0]);
  }
  const s = textOf(v).replace(/[年./]/g, '-').replace(/日/g, '');
  const m = s.match(/(\d{4})\D+(\d{1,2})\D+(\d{1,2})/);
  if (!m) return null;
  return { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) };
}

/** 在本地日历上对 {y,m,d} 加 days 天（避免 UTC 偏移） */
function addDays(parts, days) {
  if (!parts) return null;
  const dt = new Date(parts.y, parts.m - 1, parts.d);
  dt.setDate(dt.getDate() + Number(days || 0));
  return {
    y: dt.getFullYear(),
    m: dt.getMonth() + 1,
    d: dt.getDate(),
  };
}

function formatZhParts(parts) {
  if (!parts) return '';
  return `${parts.y}年${parts.m}月${parts.d}日`;
}

function formatZhDate(v) {
  const p = toDateParts(v);
  if (!p) return textOf(v);
  return formatZhParts(p);
}

/**
 * 从明细名称中提取 day 序号（1-based）。
 * 匹配：DAY1、day2、D3、第4天 等；找不到返回 null。
 */
function extractDayIndex(name) {
  const text = String(name || '');
  // DAY1 / day 2 / D3（避免把普通单词里的 d 误伤：要求 day/d 后直接跟数字）
  let m = text.match(/\bday\s*[-_]?\s*(\d+)\b/i) || text.match(/\bd\s*(\d+)\b/i);
  if (m) return Number(m[1]);
  m = text.match(/第\s*(\d+)\s*天/);
  if (m) return Number(m[1]);
  return null;
}

/**
 * 按名称中的 dayN 相对执行日计算预定日期。
 * dayIndex=1 → 执行日；dayIndex=2 → 执行日+1；无 day → 执行日。
 */
function bookingDateForItem(itemName, execDateParts) {
  if (!execDateParts) return '';
  const dayIndex = extractDayIndex(itemName);
  const offset = dayIndex != null && dayIndex >= 1 ? dayIndex - 1 : 0;
  return formatZhParts(addDays(execDateParts, offset));
}

const 订单号 = textOf(execFields['订单号']);
const 策划师 = textOf(execFields['策划']) || textOf(execFields['策划师']);

// 执行日期（基准日）；明细无 day 标记时也用这一天
const execDateRaw = execFields['执行日期'] || quote.执行日期_报价单 || '';
const execDateParts = toDateParts(execDateRaw);
const 执行日期文案 = formatZhParts(execDateParts) || formatZhDate(execDateRaw);

function buildBookingText(item) {
  const name = item?.名称 || '';
  const qty = item?.数量 != null && item?.数量 !== '' ? item.数量 : '';
  const line = qty === '' ? `1. ${name}` : `1. ${name}*${qty}`;
  // 名称含 DAY2 → 预定时间为执行日期次日，以此类推
  const 预定时间 = bookingDateForItem(name, execDateParts) || 执行日期文案;
  return [
    `趣加团队预定订单号：${订单号}`,
    `预定时间：${预定时间}`,
    '预定内容：',
    line,
    `团队策划：${策划师}`,
    '如已收到请回复预定情况，谢谢！',
  ].join('\n');
}

const 报价明细条目 = (quote.报价明细条目 || []).map((item) => ({
  ...item,
  预定信息: buildBookingText(item),
}));

// 行程：提取节点只有条目时，拼成正文供 {{行程内容}} 使用
const tripItems = quote.活动行程条目 || [];
const 活动行程 =
  quote.活动行程 ||
  tripItems
    .map((item) => {
      const day = item.日期 || item.day || '行程';
      const time = item.时间 || '';
      const content = item.内容 || '';
      return `${String(day).padEnd(6, ' ')}${String(time || '-').padEnd(14, ' ')}${content}`;
    })
    .filter((line) => line.trim())
    .join('\n');

return [
  {
    json: {
      ...quote,
      报价明细条目,
      活动行程,
      _meta: {
        exec_record_id: execItem.record_id || '',
        order_record_id: $('解析在线报价链接').first().json.order_record_id || '',
      },
    },
  },
];
