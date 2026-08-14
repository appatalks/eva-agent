#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('index.html', 'utf8');
const options = fs.readFileSync('core/js/options.js', 'utf8');
const copilot = fs.readFileSync('core/js/providers/copilot.js', 'utf8');
const bridge = fs.readFileSync('tools/bridge/core.py', 'utf8');
const routingSource = fs.readFileSync('core/js/model-routing.js', 'utf8');

function selectorValues(source) {
  const select = source.match(/<select id="selModel"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(select, 'model selector must exist');
  return [...select[1].matchAll(/value="([^"]+)"/g)].map((match) => match[1]);
}

function objectMap(source, declaration, quote) {
  const start = source.indexOf(declaration);
  assert.notStrictEqual(start, -1, `${declaration} must exist`);
  const open = source.indexOf('{', start);
  let depth = 0;
  let end = -1;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) {
      end = index + 1;
      break;
    }
  }
  assert.notStrictEqual(end, -1, `${declaration} must close`);
  const block = source.slice(open, end);
  const expression = quote === 'single'
    ? /'([^']+)':\s*'([^']+)'/g
    : /"([^"]+)":\s*"([^"]+)"/g;
  return new Map([...block.matchAll(expression)].map((match) => [match[1], match[2]]));
}

const values = selectorValues(html);
assert.strictEqual(new Set(values).size, values.length, 'selector values must be unique');
const routingSandbox = { window: {} };
vm.runInNewContext(routingSource, routingSandbox, { filename: 'core/js/model-routing.js' });
const routing = routingSandbox.window.EvaModelRouting;
assert.ok(routing, 'model routing utility must expose EvaModelRouting');

const directOpenAI = new Set([
  'gpt-4o', 'gpt-4o-mini', 'o1', 'o1-preview', 'o1-mini', 'o3-mini', 'latest'
]);
const legacyDirectOpenAI = new Set(['gpt-5-mini']);
const specialRoutes = new Map([
  ['aig', 'aigSend'],
  ['gemini', 'geminiSend'],
  ['lm-studio', 'lmsSend'],
  ['dall-e-3', 'dalle3Send']
]);

const sendStart = options.indexOf('async function sendData()');
const sendEnd = options.indexOf('\nfunction evaAuditEvent', sendStart);
assert.notStrictEqual(sendStart, -1, 'sendData must exist');
assert.notStrictEqual(sendEnd, -1, 'sendData must end before evaAuditEvent');
const sendData = options.slice(sendStart, sendEnd);

assert.match(sendData, /route === 'copilot'[\s\S]*?copilotSend\(\)/,
  'copilot selector values must route to copilotSend');
for (const model of directOpenAI) {
  assert.ok(values.includes(model), `${model} must remain selectable`);
  assert.strictEqual(routing.routeFor(model), 'openai',
    `${model} must remain in the direct OpenAI route`);
}
for (const model of legacyDirectOpenAI) {
  assert.ok(!values.includes(model), `${model} is a legacy route, not a current selector value`);
  assert.strictEqual(routing.routeFor(model), 'openai',
    `${model} must remain compatible with saved direct OpenAI selections`);
}
assert.match(sendData, /route === 'openai'[\s\S]*?trboSend\(\)/,
  'direct OpenAI selector values must route to trboSend');
assert.match(sendData, /EvaRequestRouting\.isGitHubOperation[\s\S]*?aigSend\(\)/,
  'GitHub operations must route through AIG/ACP MCP before direct model selection');
for (const [model, sender] of specialRoutes) {
  assert.ok(values.includes(model), `${model} must remain selectable`);
  assert.strictEqual(routing.routeFor(model), model === 'lm-studio' ? 'lmstudio' : model === 'dall-e-3' ? 'image' : model,
    `${model} must have an intentional sender route`);
  assert.match(sendData, new RegExp(`route === '${routing.routeFor(model)}'[\\s\\S]*?${sender}\\(\\)`),
    `${model} must route to ${sender}`);
}
for (const model of directOpenAI) {
  assert.strictEqual(routing.routeFor(model), 'openai', `${model} must route through direct OpenAI`);
}
for (const model of values.filter((value) => value.startsWith('copilot-'))) {
  assert.strictEqual(routing.routeFor(model), 'copilot', `${model} must route through Copilot`);
}
assert.strictEqual(routing.routeFor('unknown-model'), '', 'unknown models must remain invalid');
assert.strictEqual((options.match(/EvaModelRouting\.routeFor/g) || []).length, 2,
  'updateButton and sendData must share the model routing utility');

const directCopilot = values
  .filter((value) => value.startsWith('copilot-') && value !== 'copilot-acp')
  .map((value) => value.slice('copilot-'.length));
assert.ok(directCopilot.length > 0, 'at least one direct GitHub Models value must be selectable');

const browserMap = objectMap(copilot, 'var _modelMap = {', 'single');
const bridgeMap = objectMap(bridge, '_github_model_map = {', 'double');
for (const model of directCopilot) {
  assert.ok(browserMap.has(model), `${model} must map in core/js/providers/copilot.js`);
  assert.ok(bridgeMap.has(model), `${model} must map in tools/bridge/core.py`);
  assert.strictEqual(browserMap.get(model), bridgeMap.get(model),
    `${model} must use the same GitHub Models API identifier in browser and bridge routes`);
}

console.log(`model catalog tests: PASS (${values.length} selector values, ${directCopilot.length} GitHub Models values)`);