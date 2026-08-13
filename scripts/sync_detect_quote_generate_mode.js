#!/usr/bin/env node
/** Sync workflow-algorithms/detect-quote-generate-mode.js → 判定生成方式 node */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const ALGO = path.join(ROOT, 'workflow-algorithms', 'detect-quote-generate-mode.js');
const WF = path.join(ROOT, 'workflows', '测试-在线报价生成.json');

const code = fs.readFileSync(ALGO, 'utf8');
const wf = JSON.parse(fs.readFileSync(WF, 'utf8'));
const node = wf.nodes.find((n) => n.name === '判定生成方式');
if (!node) {
  console.error('Node 判定生成方式 not found');
  process.exit(1);
}
node.parameters = node.parameters || {};
node.parameters.jsCode = code;
fs.writeFileSync(WF, JSON.stringify(wf, null, 2) + '\n', 'utf8');
console.log('Synced', ALGO, '→', WF, `(${code.length} chars)`);
