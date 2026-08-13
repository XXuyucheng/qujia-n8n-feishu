#!/usr/bin/env node
/**
 * Upgrade workflows/测试-在线报价生成.json for dual-mode (attachment vs on-demand).
 * Embeds system prompt from promet/online-quote-ondemand-system.md into the Agent node.
 *
 * Usage: node scripts/upgrade_online_quote_dual_mode.js
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..');
const WF_PATH = path.join(ROOT, 'workflows', '测试-在线报价生成.json');
const PROMPT_PATH = path.join(ROOT, 'promet', 'online-quote-ondemand-system.md');

function uuid() {
  return crypto.randomUUID();
}

function loadSystemMessage() {
  const raw = fs.readFileSync(PROMPT_PATH, 'utf8');
  const idx = raw.indexOf('你是');
  if (idx < 0) {
    throw new Error('prompt file missing body starting with 你是');
  }
  return raw.slice(idx).trim();
}

function selectText(field) {
  return `
function selectFieldText(field) {
  if (field == null) return '';
  if (typeof field === 'string') return field.trim();
  if (typeof field === 'number') return String(field);
  if (Array.isArray(field)) {
    const first = field[0];
    if (first == null) return '';
    if (typeof first === 'string') return first.trim();
    if (typeof first === 'object') {
      return String(first.text || first.name || first.id || '').trim();
    }
  }
  if (typeof field === 'object') {
    if (typeof field.text === 'string') return field.text.trim();
    if (typeof field.name === 'string') return field.name.trim();
    if (Array.isArray(field.value) && field.value.length) {
      return selectFieldText(field.value);
    }
  }
  return '';
}

function selectFieldId(field) {
  if (field == null) return '';
  if (Array.isArray(field) && field[0]) {
    return String(field[0].id || field[0].option_id || '').trim();
  }
  if (typeof field === 'object') {
    return String(field.id || field.option_id || '').trim();
  }
  return '';
}
`.trim();
}

const JUDGE_CODE = `${selectText()}

/**
 * 生成方式配置（补齐真实 id 后替换 PLACEHOLDER）
 * 字段名优先匹配；也可用 field_id / option id
 */
const MODE_CONFIG = {
  fieldNames: ['报价生成方式', '生成方式'],
  // TODO: 换成真实 field_id，例如 'fldXXXXXXXX'
  fieldIds: ['FIELD_QUOTE_MODE_PLACEHOLDER'],
  attachment: {
    names: ['附件生成'],
    // TODO: 换成真实 opt id
    ids: ['OPT_ATTACHMENT_PLACEHOLDER'],
  },
  ondemand: {
    names: ['按需生成'],
    ids: ['OPT_ONDEMAND_PLACEHOLDER'],
  },
};

const recordId = $('解析触发记录').first().json.record_id;
const resp = $input.first().json;
const data = resp.data || resp;
const record = data.record || data;
const fields = record.fields || {};

function pickModeField() {
  for (const name of MODE_CONFIG.fieldNames) {
    if (fields[name] != null) return fields[name];
  }
  // 少数情况下 API 只回 field_id（本仓库读取接口一般用字段名）
  for (const id of MODE_CONFIG.fieldIds) {
    if (id && !id.includes('PLACEHOLDER') && fields[id] != null) return fields[id];
  }
  return null;
}

const modeField = pickModeField();
const modeText = selectFieldText(modeField);
const modeOptId = selectFieldId(modeField);

let generate_mode = '';
if (
  MODE_CONFIG.ondemand.names.includes(modeText) ||
  MODE_CONFIG.ondemand.ids.includes(modeOptId)
) {
  generate_mode = 'ondemand';
} else if (
  MODE_CONFIG.attachment.names.includes(modeText) ||
  MODE_CONFIG.attachment.ids.includes(modeOptId)
) {
  generate_mode = 'attachment';
}

if (!generate_mode) {
  throw new Error(
    '无法判定生成方式：请在订单填写「报价生成方式」= 附件生成 / 按需生成' +
      (modeText ? ('（当前值：' + modeText + '）') : '（当前为空）'),
  );
}

function textField(name) {
  const v = fields[name];
  if (v == null) return '';
  if (typeof v === 'string' || typeof v === 'number') return String(v);
  if (Array.isArray(v)) return String(v[0]?.text || v[0] || '');
  if (typeof v === 'object' && v.text != null) return String(v.text);
  return '';
}

const 订单号 = textField('订单号') || textField('订单编号') || recordId;
const 客户需求 = textField('客户需求');

return [{
  json: {
    record_id: record.record_id || recordId,
    订单号,
    generate_mode,
    is_attachment: generate_mode === 'attachment',
    is_ondemand: generate_mode === 'ondemand',
    mode_text: modeText,
    客户需求,
    活动名称: textField('活动名称'),
    报价人数: textField('报价人数') || textField('执行人数'),
    活动天数: textField('活动天数'),
    活动日期: textField('活动日期') || textField('执行日期'),
    出发地目的地: textField('出发地目的地') || textField('目的地'),
    fields,
  },
}];
`;

const ONDEMAND_CTX_CODE = `const base = $input.first().json;
if (base.generate_mode !== 'ondemand') {
  throw new Error('内部错误：按需分支收到非 ondemand');
}
if (!String(base.客户需求 || '').trim()) {
  throw new Error('按需生成要求填写「客户需求」');
}

const userPrompt = [
  '【订单已知字段】',
  '订单号：' + (base.订单号 || ''),
  '活动名称：' + (base.活动名称 || ''),
  '报价人数：' + (base.报价人数 || ''),
  '活动天数：' + (base.活动天数 || ''),
  '活动日期：' + (base.活动日期 || ''),
  '出发地目的地：' + (base.出发地目的地 || ''),
  '',
  '【客户需求】',
  base.客户需求,
  '',
  '请按系统要求只输出一个 JSON 对象。',
].join('\\n');

return [{
  json: {
    ...base,
    user_prompt: userPrompt,
  },
}];
`;

const PARSE_AI_CODE = `const base = $('组装按需上下文').first().json;
const raw = $json.output ?? $json.text ?? $json.response ?? '';

function extractJson(text) {
  const cleaned = String(text || '').replace(/\`\`\`json/gi, '').replace(/\`\`\`/g, '').trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) {
    throw new Error('AI 未返回 JSON 对象');
  }
  return JSON.parse(cleaned.slice(start, end + 1));
}

let data;
try {
  data = extractJson(raw);
} catch (e) {
  throw new Error('解析按需生成 AI 结果失败: ' + (e.message || e) + '\\n原始输出: ' + String(raw).slice(0, 500));
}

const sheet_rows = Array.isArray(data.sheet_rows) ? data.sheet_rows : [];
const itinerary_rows = Array.isArray(data.itinerary_rows) ? data.itinerary_rows : [];

const normalizedSheet = sheet_rows
  .filter((r) => r && typeof r === 'object')
  .map((r) => ({
    类目: String(r.类目 || r.分类 || '其他'),
    物品名称: String(r.物品名称 || r.名称 || ''),
    描述: String(r.描述 || r.内容 || ''),
    数量: r.数量 == null || r.数量 === '' ? null : Number(r.数量),
    单价: null, // 价目由策划人工填写
  }))
  .filter((r) => r.物品名称 || r.描述);

const normalizedItinerary = itinerary_rows
  .filter((r) => r && typeof r === 'object')
  .map((r) => ({
    日期: String(r.日期 || r.date || ''),
    时间: String(r.时间 || r.time || ''),
    内容: String(r.内容 || r.content || r.描述 || ''),
  }))
  .filter((r) => r.日期 || r.时间 || r.内容);

return [{
  json: {
    record_id: base.record_id,
    订单号: base.订单号,
    generate_mode: 'ondemand',
    活动名称: data.活动名称 || base.活动名称 || '',
    报价人数: data.报价人数 ?? base.报价人数 ?? '',
    活动天数: data.活动天数 || base.活动天数 || '',
    活动日期: data.活动日期 || base.活动日期 || '',
    出发地目的地: data.出发地目的地 || base.出发地目的地 || '',
    税费及服务: (data.税费及服务 && String(data.税费及服务).trim()) || '5%',
    sheet_rows: normalizedSheet,
    itinerary_rows: normalizedItinerary,
    报价明细条目: normalizedSheet,
    行程明细: normalizedItinerary,
  },
}];
`;

function main() {
  const systemMessage = loadSystemMessage();
  const wf = JSON.parse(fs.readFileSync(WF_PATH, 'utf8'));

  // Remove previous upgrade nodes if re-run
  const drop = new Set([
    '判定生成方式',
    'Switch生成方式',
    '组装按需上下文',
    'DeepSeek Agent_按需报价',
    'DeepSeek Chat Model_按需报价',
    '解析按需明细',
  ]);
  wf.nodes = wf.nodes.filter((n) => !drop.has(n.name));

  const readNode = wf.nodes.find((n) => n.name === '读取订单记录');
  const baseX = readNode?.position?.[0] ?? 176;
  const baseY = readNode?.position?.[1] ?? -112;

  const judgeId = uuid();
  const switchId = uuid();
  const ctxId = uuid();
  const agentId = uuid();
  const modelId = uuid();
  const parseId = uuid();

  const newNodes = [
    {
      parameters: { jsCode: JUDGE_CODE },
      id: judgeId,
      name: '判定生成方式',
      type: 'n8n-nodes-base.code',
      typeVersion: 2,
      position: [baseX + 220, baseY],
    },
    {
      parameters: {
        rules: {
          values: [
            {
              conditions: {
                options: {
                  caseSensitive: true,
                  leftValue: '',
                  typeValidation: 'strict',
                  version: 3,
                },
                conditions: [
                  {
                    id: uuid(),
                    leftValue: '={{ $json.is_attachment }}',
                    rightValue: '',
                    operator: {
                      type: 'boolean',
                      operation: 'true',
                      singleValue: true,
                    },
                  },
                ],
                combinator: 'and',
              },
              renameOutput: true,
              outputKey: '附件生成',
            },
            {
              conditions: {
                options: {
                  caseSensitive: true,
                  leftValue: '',
                  typeValidation: 'strict',
                  version: 3,
                },
                conditions: [
                  {
                    id: uuid(),
                    leftValue: '={{ $json.is_ondemand }}',
                    rightValue: '',
                    operator: {
                      type: 'boolean',
                      operation: 'true',
                      singleValue: true,
                    },
                  },
                ],
                combinator: 'and',
              },
              renameOutput: true,
              outputKey: '按需生成',
            },
          ],
        },
        options: {},
      },
      id: switchId,
      name: 'Switch生成方式',
      type: 'n8n-nodes-base.switch',
      typeVersion: 3.4,
      position: [baseX + 440, baseY],
    },
    {
      parameters: { jsCode: ONDEMAND_CTX_CODE },
      id: ctxId,
      name: '组装按需上下文',
      type: 'n8n-nodes-base.code',
      typeVersion: 2,
      position: [baseX + 440, baseY + 220],
    },
    {
      parameters: {
        promptType: 'define',
        text: '={{ $json.user_prompt }}',
        options: {
          systemMessage,
        },
      },
      id: agentId,
      name: 'DeepSeek Agent_按需报价',
      type: '@n8n/n8n-nodes-langchain.agent',
      typeVersion: 1.7,
      position: [baseX + 680, baseY + 220],
      continueOnFail: false,
    },
    {
      parameters: {
        model: 'deepseek-v4-flash',
        options: {},
      },
      id: modelId,
      name: 'DeepSeek Chat Model_按需报价',
      type: '@n8n/n8n-nodes-langchain.lmChatDeepSeek',
      typeVersion: 1,
      position: [baseX + 680, baseY + 420],
      credentials: {
        deepSeekApi: {
          id: 'PyhZGcLIC7RuJz4H',
          name: 'DeepSeek account',
        },
      },
    },
    {
      parameters: { jsCode: PARSE_AI_CODE },
      id: parseId,
      name: '解析按需明细',
      type: 'n8n-nodes-base.code',
      typeVersion: 2,
      position: [baseX + 920, baseY + 220],
    },
  ];

  // Shift attachment chain nodes a bit right for layout clarity (optional)
  const attachNames = [
    '校验报价附件',
    '获取临时下载链接',
    '下载文件',
    'Extract from File',
    '报价信息提取',
    '行程信息提取',
  ];
  for (const n of wf.nodes) {
    if (attachNames.includes(n.name) && Array.isArray(n.position)) {
      n.position[0] = Math.max(n.position[0], baseX + 660);
      n.position[1] = baseY - 20;
    }
  }

  // 附件分支入口不再是「读取订单记录」直连，改为从该节点取数
  const validate = wf.nodes.find((n) => n.name === '校验报价附件');
  if (validate?.parameters?.jsCode) {
    validate.parameters.jsCode = validate.parameters.jsCode.replace(
      'const resp = $input.first().json;',
      "const resp = $('读取订单记录').first().json;",
    );
  }

  const insertAfter = wf.nodes.findIndex((n) => n.name === '读取订单记录');
  wf.nodes.splice(insertAfter + 1, 0, ...newNodes);

  // Rewire
  wf.connections['读取订单记录'] = {
    main: [[{ node: '判定生成方式', type: 'main', index: 0 }]],
  };
  wf.connections['判定生成方式'] = {
    main: [[{ node: 'Switch生成方式', type: 'main', index: 0 }]],
  };
  // Switch output 0 = 附件, output 1 = 按需
  wf.connections['Switch生成方式'] = {
    main: [
      [{ node: '校验报价附件', type: 'main', index: 0 }],
      [{ node: '组装按需上下文', type: 'main', index: 0 }],
    ],
  };
  wf.connections['组装按需上下文'] = {
    main: [[{ node: 'DeepSeek Agent_按需报价', type: 'main', index: 0 }]],
  };
  wf.connections['DeepSeek Agent_按需报价'] = {
    main: [[{ node: '解析按需明细', type: 'main', index: 0 }]],
  };
  wf.connections['解析按需明细'] = {
    main: [[{ node: '组装生成请求', type: 'main', index: 0 }]],
  };
  // AI model connection
  wf.connections['DeepSeek Chat Model_按需报价'] = {
    ai_languageModel: [
      [{ node: 'DeepSeek Agent_按需报价', type: 'ai_languageModel', index: 0 }],
    ],
  };

  // Keep 行程信息提取 -> 组装生成请求
  wf.name = '测试-在线报价生成';
  wf.versionId = uuid();

  fs.writeFileSync(WF_PATH, JSON.stringify(wf, null, 2) + '\n', 'utf8');
  console.log('Updated', WF_PATH);
  console.log('System message chars:', systemMessage.length);
  console.log('Nodes:', wf.nodes.map((n) => n.name).join(' | '));
}

main();
