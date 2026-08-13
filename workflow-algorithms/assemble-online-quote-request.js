/**
 * 组装在线报价生成请求（报价提取 / 按需生成 → 组装生成请求）
 *
 * 职责：
 * 1. 从上游报价结果 + 订单记录拼装 /api/generate 请求体
 * 2. 按策划师 open_id 映射输出目录 folder_token
 * 3. 按生成方式校验；失败时输出短错误码（供 IF 分流后回写「报价生成状态」）
 *
 * 校验分流：
 *   - 附件生成：人数 + 策划师/输出目录
 *   - 按需生成：订单号/出发地/目的地/出发日期/活动天数 + 策划师/输出目录
 *   （缺策划师 / 缺输出目录：两种方式都校验）
 *
 * 同步：node scripts/sync_assemble_online_quote_request.js
 * 或手动粘贴到 n8n「组装生成请求」Code 节点
 */

const q = $input.first().json;
const token = $('获取飞书Token').first().json.tenant_access_token || '';

/** 策划师 open_id → 飞书云空间输出目录 folder_token */
const PLANNER_OUTPUT_FOLDER = {
  'ou_31f3068ea0ae6fa7ffb24afaa2715c2d': 'OvlefG0vnl8DVudsbgyc5CUpndb',
  'ou_20aacc78880fafd1e978064db74b8caa': 'BDTPfGghllN9HVd5CQacURbGnZe',
  // 继续加：'ou_xxx': 'folderTokenXxx',
};

const orderRecord = $('读取订单记录').first().json.data.record || {};
const orderFields = orderRecord.fields || {};
const recordId =
  q.record_id ||
  orderRecord.record_id ||
  $('解析触发记录').first().json.record_id ||
  '';

const plannerId =
  orderFields?.策划师?.[0]?.id ||
  orderFields?.策划师?.[0]?.open_id ||
  '';
const folder_token = PLANNER_OUTPUT_FOLDER[plannerId] || '';

/** 飞书多维表格字段 / 字符串清洗 */
function textOf(v) {
  if (v == null || v === '') return '';
  if (typeof v === 'string' || typeof v === 'number') return String(v).trim();
  if (Array.isArray(v)) {
    return v
      .map((x) => (x && (x.text ?? x.name ?? x)) || '')
      .map((s) => String(s).trim())
      .filter(Boolean)
      .join('');
  }
  if (typeof v === 'object' && v.text != null) return String(v.text).trim();
  return String(v).trim();
}

/** 生成方式：上游透传 → 判定生成方式节点 → 订单「报价生成状态」 */
function resolveGenerateMode() {
  const fromQ = q.generate_mode || (q.is_attachment ? 'attachment' : '') || (q.is_ondemand ? 'ondemand' : '');
  if (fromQ === 'attachment' || fromQ === 'ondemand') return fromQ;
  try {
    const d = $('判定生成方式').first().json || {};
    if (d.generate_mode === 'attachment' || d.generate_mode === 'ondemand') {
      return d.generate_mode;
    }
  } catch (_) {
    /* 节点未执行时忽略 */
  }
  const status = textOf(orderFields['报价生成状态']);
  if (status.includes('按需')) return 'ondemand';
  if (status.includes('附件')) return 'attachment';
  return '';
}

const generate_mode = resolveGenerateMode();

// —— 业务字段（优先报价提取 / 按需结果，回退订单表）——
const 订单号 =
  textOf(q.订单号) ||
  textOf(orderFields['订单号']) ||
  textOf(orderFields['订单编号']);

const 报价人数Raw = q.报价人数 ?? orderFields['报价人数'] ?? orderFields['执行人数'];
const 报价人数Num = Number(
  typeof 报价人数Raw === 'object' && 报价人数Raw != null
    ? 报价人数Raw.text ?? 报价人数Raw
    : 报价人数Raw,
);

const 活动天数 =
  textOf(q.活动天数) ||
  textOf(orderFields['活动天数']) ||
  textOf(orderFields['天数']);

let 出发地 = textOf(q.出发地) || textOf(orderFields['出发地']);
let 目的地 = textOf(q.目的地) || textOf(orderFields['目的地']);
const 出发地目的地 =
  textOf(q.出发地目的地) || textOf(orderFields['出发地目的地']);
// 「上海-舟山」一类合并字段拆成出发地 / 目的地
if ((!出发地 || !目的地) && 出发地目的地) {
  const parts = 出发地目的地
    .split(/[-—–～~]/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (!出发地 && parts[0]) 出发地 = parts[0];
  if (!目的地 && parts[1]) 目的地 = parts[1];
}

const 出发日期 =
  textOf(q.活动日期) ||
  textOf(q.执行日期) ||
  textOf(q.出发日期) ||
  textOf(orderFields['执行日期']) ||
  textOf(orderFields['活动日期']);

// —— 校验：附件校人数；按需校业务字段；策划师/输出目录两种方式都校 ——
const errorCodes = [];
const isAttachment = generate_mode === 'attachment';
const isOndemand = generate_mode === 'ondemand';

if (isAttachment) {
  if (!Number.isFinite(报价人数Num) || 报价人数Num <= 0) errorCodes.push('缺人数');
} else if (isOndemand) {
  if (!订单号) errorCodes.push('缺订单号');
  if (!出发地) errorCodes.push('缺出发地');
  if (!目的地) errorCodes.push('缺目的地');
  if (!出发日期) errorCodes.push('缺出发日期');
  if (!活动天数) errorCodes.push('缺活动天数');
} else {
  // 方式未知时保守：两边业务规则都做
  if (!Number.isFinite(报价人数Num) || 报价人数Num <= 0) errorCodes.push('缺人数');
  if (!订单号) errorCodes.push('缺订单号');
  if (!出发地) errorCodes.push('缺出发地');
  if (!目的地) errorCodes.push('缺目的地');
  if (!出发日期) errorCodes.push('缺出发日期');
  if (!活动天数) errorCodes.push('缺活动天数');
}

if (!plannerId) errorCodes.push('缺策划师');
else if (!folder_token) errorCodes.push('缺输出目录');

const validation_ok = errorCodes.length === 0;
const error_code = validation_ok ? '' : errorCodes[0];

return [
  {
    json: {
      // IF 分流：true → 生成在线报价；false → HTTP 回写 error_code 到「报价生成状态」
      validation_ok,
      error_code,
      error_codes: errorCodes,
      generate_mode,

      record_id: recordId,
      tenant_access_token: token,
      signing_unit: '在线报价',
      folder_token,

      fields: {
        订单号: 订单号 || '',
        活动名称: q.活动名称 || '',
        报价人数:
          Number.isFinite(报价人数Num) && 报价人数Num > 0
            ? 报价人数Num
            : q.报价人数 ?? '',
        活动天数: 活动天数 || q.活动天数 || '',
        活动日期: 出发日期 || q.活动日期 || '',
        出发地,
        目的地,
        出发地目的地:
          出发地目的地 || [出发地, 目的地].filter(Boolean).join('-'),
        税费及服务: q.税费及服务 || '',
      },
      sheet_rows: q.sheet_rows || [],
      itinerary_rows: q.itinerary_rows || [],

      _meta: {
        record_id: recordId,
        策划师id: plannerId,
        folder_token,
        generate_mode,
        validation_ok,
        error_code,
        error_codes: errorCodes,
      },
    },
  },
];
