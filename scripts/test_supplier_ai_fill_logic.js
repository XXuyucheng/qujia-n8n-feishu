const assert = require('assert');
const {
  pickText,
  buildUpdateForRecord,
  parseAiBatch,
  alignAndBuildUpdates,
  needsFill,
} = require('./supplier_ai_fill_logic');

function test(name, fn) {
  try {
    fn();
    console.log(`PASS ${name}`);
  } catch (e) {
    console.error(`FAIL ${name}`);
    throw e;
  }
}

test('pickText unwraps feishu text array', () => {
  assert.strictEqual(pickText([{ text: '杭州酒店', type: 'text' }]), '杭州酒店');
  assert.strictEqual(pickText(['现结']), '现结');
  assert.strictEqual(pickText(null), '');
});

test('only fill empty — do not overwrite', () => {
  const r = buildUpdateForRecord(
    { 类型: '酒店', 地域: '', 资质: '其他' },
    { 类型: '餐厅', 地域: '德清', 资质: '餐饮', low_confidence: [] },
  );
  assert.strictEqual(r.fields['类型'], undefined);
  assert.strictEqual(r.fields['地域'], '德清');
  assert.strictEqual(r.fields['资质'], undefined);
  assert.strictEqual(r.aiMark, null);
});

test('invalid type discarded and marked for review', () => {
  const r = buildUpdateForRecord(
    { 类型: '', 地域: '杭州', 资质: '其他' },
    { 类型: '乱写', 地域: '杭州', 资质: '其他', low_confidence: [] },
  );
  assert.strictEqual(r.fields['类型'], undefined);
  assert.ok(String(r.fields['AI 标记']).includes('类型'));
});

test('custom region allowed and marked AI 待核', () => {
  const r = buildUpdateForRecord(
    { 类型: '酒店', 地域: '', 资质: '其他' },
    { 类型: '酒店', 地域: '富阳', 资质: '其他', low_confidence: [] },
  );
  assert.strictEqual(r.fields['地域'], '富阳');
  assert.strictEqual(r.fields['AI 标记'], 'AI 待核：地域');
});

test('preferred region without low_confidence has no AI mark', () => {
  const r = buildUpdateForRecord(
    { 类型: '', 地域: '', 资质: '' },
    {
      类型: '酒店',
      地域: '杭州',
      资质: '其他',
      low_confidence: [],
    },
  );
  assert.strictEqual(r.fields['类型'], '酒店');
  assert.strictEqual(r.fields['地域'], '杭州');
  assert.strictEqual(r.fields['资质'], '其他');
  assert.strictEqual(r.aiMark, null);
  assert.strictEqual(r.fields['AI 标记'], undefined);
});

test('low_confidence marks fields', () => {
  const r = buildUpdateForRecord(
    { 类型: '', 地域: '', 资质: '' },
    {
      类型: '民宿',
      地域: '安吉',
      资质: '个人',
      low_confidence: ['类型', '资质'],
    },
  );
  assert.strictEqual(r.fields['AI 标记'], 'AI 待核：类型/资质');
});

test('景区 fullwidth paren normalized', () => {
  const r = buildUpdateForRecord(
    { 类型: '', 地域: '千岛湖', 资质: '其他' },
    { 类型: '景区（基地）', 地域: '千岛湖', 资质: '其他' },
  );
  assert.strictEqual(r.fields['类型'], '景区 (基地)');
});

test('parseAiBatch strips markdown fence', () => {
  const arr = parseAiBatch('```json\n[{"record_id":"r1","类型":"酒店"}]\n```');
  assert.strictEqual(arr[0].record_id, 'r1');
});

test('alignAndBuildUpdates uses item record_id by index', () => {
  const batch = [
    { record_id: 'recA', fields: { 类型: '', 地域: '', 资质: '' } },
    { record_id: 'recB', fields: { 类型: '酒店', 地域: '杭州', 资质: '其他' } },
    { record_id: 'recC', fields: { 类型: '', 地域: '', 资质: '' } },
  ];
  const ai = [
    { 类型: '酒店', 地域: '杭州', 资质: '其他', low_confidence: [] },
    { 类型: '餐厅', 地域: '德清', 资质: '餐饮', low_confidence: [] },
    { 类型: '民宿', 地域: '安吉', 资质: '个人', low_confidence: [] },
  ];
  const { updates, errors } = alignAndBuildUpdates(batch, ai);
  assert.strictEqual(updates.length, 2);
  assert.strictEqual(updates[0].record_id, 'recA');
  assert.strictEqual(updates[1].record_id, 'recC');
  assert.ok(errors.some((e) => e.record_id === 'recB' && e.error === '无可写字段'));
});

test('alignAndBuildUpdates ignores AI record_id', () => {
  const batch = [{ record_id: 'from-item', fields: { 类型: '', 地域: '杭州', 资质: '其他' } }];
  const ai = [{ record_id: 'from-ai-wrong', 类型: '酒店', 地域: '杭州', 资质: '其他' }];
  const { updates } = alignAndBuildUpdates(batch, ai);
  assert.strictEqual(updates[0].record_id, 'from-item');
});

test('needsFill', () => {
  assert.strictEqual(needsFill({ 类型: '酒店', 地域: '杭州', 资质: '其他' }), false);
  assert.strictEqual(needsFill({ 类型: '', 地域: '杭州', 资质: '其他' }), true);
  assert.strictEqual(needsFill({}), true);
});

console.log('All tests passed.');
