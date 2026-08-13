#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const elements = {
  dataModeStatus: { textContent: '' },
  selDataMode: { value: 'cloud' },
  doctorButton: { disabled: false },
  doctorReport: { textContent: '', style: {} }
};
const storage = new Map();
const calls = [];
const responses = [];
const context = {
  AbortSignal,
  JSON,
  Promise,
  localStorage: {
    getItem(key) { return storage.get(key) || null; },
    setItem(key, value) { storage.set(key, String(value)); }
  },
  getACPBridgeUrl() { return 'http://localhost:8888/'; },
  async detectACPBridge() { return 'http://localhost:8999/'; },
  document: { getElementById(id) { return elements[id] || null; } },
  fetch(url, options) {
    calls.push({ url, options });
    return Promise.resolve(responses.shift());
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/settings/runtime.js', 'utf8'), context, {
  filename: 'core/js/settings/runtime.js'
});

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(function(resolve) { setImmediate(resolve); });
}

async function main() {
  responses.push({ json: async () => ({ mode: 'local', local_tools: 3 }) });
  context.switchDataMode('local');
  await flush();
  assert.strictEqual(calls[0].url, 'http://localhost:8888/v1/mode');
  assert.strictEqual(calls[0].options.method, 'POST');
  assert.deepStrictEqual(JSON.parse(calls[0].options.body), { mode: 'local' });
  assert.strictEqual(elements.selDataMode.value, 'local');
  assert.strictEqual(storage.get('evaDataMode'), 'local');
  assert.match(elements.dataModeStatus.textContent, /Local mode active \(3 MCP tools available\)/);

  responses.push({ json: async () => ({ mode: 'cloud', cloud_available: true, local_available: false, local_tools: 0 }) });
  context.loadDataMode();
  await flush();
  assert.strictEqual(calls[1].url, 'http://localhost:8888/v1/mode');
  assert.strictEqual(elements.selDataMode.value, 'cloud');
  assert.strictEqual(storage.get('evaDataMode'), 'cloud');
  assert.strictEqual(elements.dataModeStatus.textContent, 'Cloud: available | Local: not started');

  const report = context.formatDoctorReport({
    readiness: { can_chat: true, can_browse: false },
    blockers: ['Copilot authentication is required'],
    subsystems: { system: { python: '3.12', node: '24', platform: 'linux', arch: 'x64' }, mcp: { configured: ['web-search'] } }
  });
  assert.match(report, /Chat \(ACP\)/);
  assert.match(report, /Browser agent/);
  assert.match(report, /System: Python 3.12, Node 24/);
  assert.match(report, /MCP servers: web-search/);
  assert.match(report, /Blockers:/);

  responses.push({ ok: true, json: async () => ({ readiness: {}, blockers: [], subsystems: {} }) });
  await context.runDoctor();
  assert.strictEqual(calls[2].url, 'http://localhost:8999/v1/doctor');
}

main().then(function() {
  console.log('runtime settings tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});