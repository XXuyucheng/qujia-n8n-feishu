/**
 * 出团计划单 — 出团信息聚合
 *
 * 职责：
 * 1. 从执行表 + 预定信息生成结果组装生成请求（fields / sheet_rows）
 * 2. 按策划师 open_id 映射输出目录 folder_token（与「在线报价生成」同一套表）
 * 3. 缺策划师 / 缺输出目录时 ok=false，供 If 分流后回写「计划单生成状态」
 *
 * 依赖节点：
 *   - $input ← 「预定信息生成」
 *   - $('查询待识别执行记录')
 *   - $('解析在线报价链接')
 *   - $('获取飞书Token')
 *
 * 使用：粘贴到 n8n「出团信息聚合」Code 节点；与 workflows/出团计划单生成-在线报价.json 同步维护。
 */

const quote = $input.first().json; // 来自「预定信息生成」
const execResp = $('查询待识别执行记录').first().json;
const execItem = execResp?.data?.items?.[0] || {};
const execFields = execItem.fields || {};
const execRecordId = quote._meta?.exec_record_id || execItem.record_id || '';
const orderRecordId =
  quote._meta?.order_record_id ||
  $('解析在线报价链接').first().json.order_record_id ||
  '';
const token = $('获取飞书Token').first().json.tenant_access_token || '';

/** 策划师 open_id → 飞书云空间输出目录 folder_token（与在线报价生成一致） */
const PLANNER_OUTPUT_FOLDER = {
  'ou_31f3068ea0ae6fa7ffb24afaa2715c2d': 'OvlefG0vnl8DVudsbgyc5CUpndb',
  'ou_a67e7c9602d7f44315ecd5a29adc912c': 'BDTPfGghllN9HVd5CQacURbGnZe',
};

const errors = [];

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
  if (typeof v === 'number') {
    return new Date(v).toISOString().slice(0, 10).replace(/-/g, '/');
  }
  if (
    typeof v === 'object' &&
    Array.isArray(v.value) &&
    typeof v.value[0] === 'number'
  ) {
    return new Date(v.value[0]).toISOString().slice(0, 10).replace(/-/g, '/');
  }
  return textOf(v).replace(/-/g, '/');
}

function isPercentFeeItem(item) {
  const text = [item?.名称, item?.描述, item?.分类]
    .map((s) => String(s || '').trim())
    .join(' ');
  if (!text) return false;
  return /税费|服务费|管理费|税率|增值税|附加费|[%％]|百分比/.test(text);
}

function itemsByCategory(items, keyword) {
  return items.filter((item) => String(item.分类 || '').includes(keyword));
}

function joinItemNames(items, sep = '；') {
  return items
    .map((item) => `${item.名称 || ''}${item.数量 != null ? `×${item.数量}` : ''}`)
    .filter(Boolean)
    .join(sep);
}

const plannerField = execFields['策划'] || execFields['策划师'] || [];
const plannerId =
  (Array.isArray(plannerField) ? plannerField[0] : null)?.id ||
  (Array.isArray(plannerField) ? plannerField[0] : null)?.open_id ||
  '';
const folder_token = PLANNER_OUTPUT_FOLDER[plannerId] || '';

if (!plannerId) errors.push('缺少策划师');
else if (!folder_token) errors.push('缺少输出目录');

const 报价明细条目 = (quote.报价明细条目 || []).filter(
  (item) => !isPercentFeeItem(item),
);

const customerRaw = textOf(execFields['客户信息']);
const parts = customerRaw
  .split(/[；;]/)
  .map((s) => s.trim())
  .filter(Boolean);
const 单位 = parts[0] || textOf(execFields['单位']);
const 联系人 = parts[1] || textOf(execFields['联系人']);
const 联系方式 = parts[2] || textOf(execFields['联系方式']);

const sheet_rows = 报价明细条目.map((item) => ({
  名称: item.名称 || '',
  描述: item.描述 || '',
  数量: item.数量,
  单价: item.单价,
  结算方式: item.结算方式 || '',
  联系人: item.联系人 || '',
  预定信息: item.预定信息 || '',
}));

const 工作人员安排 = joinItemNames(itemsByCategory(报价明细条目, '人员安排'));
const 活动项目 = joinItemNames(itemsByCategory(报价明细条目, '活动选配'), '\t');
const 车型 = joinItemNames(itemsByCategory(报价明细条目, '交通'));

const 导游 = textOf(execFields['导游']);
const 摄影 = textOf(execFields['摄影']);
const 教练 = textOf(execFields['教练']);
const 导游摄影 = [导游, 摄影].filter(Boolean).join('；');
const 工作人员 = [工作人员安排, 导游摄影].filter(Boolean).join('；');

const ok = errors.length === 0;
const errorMessage = errors.join('；');

return [
  {
    json: {
      ok,
      errors,
      error_message: errorMessage,
      planner_id: plannerId,
      tenant_access_token: token,
      signing_unit: '出团计划单',
      folder_token,
      fields: {
        订单号: textOf(execFields['订单号']),
        单位,
        联系人,
        联系方式,
        客户对接人: [联系人, 联系方式].filter(Boolean).join('：'),
        工作人员,
        车型,
        教练,
        活动项目,
        策划师: textOf(execFields['策划']) || textOf(execFields['策划师']),
        执行日期:
          dateOf(execFields['执行日期']) || quote.执行日期_报价单 || '',
        执行人数: quote.人数 || textOf(execFields['执行人数']),
        活动名称: quote.活动名称 || '',
        行程内容: quote.活动行程 || '',
        报价明细条目,
        执行编号: textOf(execFields['执行编号']),
        执行记录_id: execRecordId,
        订单记录_id: orderRecordId,
      },
      sheet_rows,
    },
  },
];
