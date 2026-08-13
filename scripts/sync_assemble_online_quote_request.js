#!/usr/bin/env node
/** Sync workflow-algorithms/assemble-online-quote-request.js → 组装生成请求 node */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const ALGO = path.join(ROOT, 'workflow-algorithms', 'assemble-online-quote-request.js');
const WF = path.join(ROOT, 'workflows', '测试-在线报价生成.json');

const code = fs.readFileSync(ALGO, 'utf8');
const wf = JSON.parse(fs.readFileSync(WF, 'utf8'));
const node = wf.nodes.find((n) => n.name === '组装生成请求');
if (!node) {
  console.error('Node 组装生成请求 not found');
  process.exit(1);
}
node.parameters = node.parameters || {};
node.parameters.jsCode = code;
fs.writeFileSync(WF, JSON.stringify(wf, null, 2) + '\n', 'utf8');
console.log('Synced', ALGO, '→', WF, `(${code.length} chars)`);
