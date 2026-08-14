#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

let now = 100000;
let standalone = true;
const timers = [];
const requests = [];
const responses = [];
const output = { children: [], appendChild(node) { this.children.push(node); }, scrollTop: 0, scrollHeight: 10 };
function element(tag) {
  return {
    tag,
    children: [],
    parentNode: null,
    appendChild(child) { child.parentNode = this; this.children.push(child); },
    removeChild(child) { this.children = this.children.filter((item) => item !== child); },
    addEventListener(type, handler) { this[type] = handler; }
  };
}
const context = {
  Array,
  Date: { now() { return now; } },
  JSON,
  Math,
  Number,
  Promise,
  String,
  clearTimeout() {},
  setTimeout(handler, delay) { timers.push({ handler, delay }); return timers.length; },
  encodeURIComponent,
  isEvaStandalone() { return standalone; },
  getBridgeCapabilityHeaders() { return { 'X-Eva-Bridge-Capability': 'test-capability' }; },
  setStatus() {},
  document: {
    getElementById(id) { return id === 'txtOutput' ? output : null; },
    createElement(tag) { return element(tag); }
  },
  backgroundBridgeRequest(path, options) {
    requests.push({ path, options });
    const next = responses.shift();
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(next || {});
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/features/permissions/acp.js', 'utf8'), context, {
  filename: 'core/js/features/permissions/acp.js'
});

async function main() {
  assert.strictEqual(context._acpPermissionPollDelay(), 300000);
  context._acpPermissionState.activeUntil = now + 1;
  assert.strictEqual(context._acpPermissionPollDelay(), 30000);
  context._acpPermissionState.pending = true;
  assert.strictEqual(context._acpPermissionPollDelay(), 3000);
  context._acpPermissionState.pending = false;

  context.watchACPPermissions(45000);
  assert.strictEqual(context._acpPermissionState.activeUntil, now + 45000);
  assert.strictEqual(timers.pop().delay, 0);

  const permission = {
    id: 'perm/1', tool_kind: 'execute', command_summary: 'git status', approval_allowed: false,
    options: [{ kind: 'allow_once' }, { kind: 'reject_once' }]
  };
  responses.push({ permissions: [permission] });
  await context.pollACPPermissions();
  assert.strictEqual(requests[0].path, '/v1/acp/permissions');
  assert.deepStrictEqual(requests[0].options.headers, { 'X-Eva-Bridge-Capability': 'test-capability' });
  assert.strictEqual(output.children.length, 1);
  const actions = output.children[0].children[1];
  assert.strictEqual(actions.children[0].disabled, true, 'approval policy must disable Allow once');
  assert.strictEqual(context._acpPermissionState.pending, true);
  assert.strictEqual(context._acpPermissionState.polling, false);
  assert.strictEqual(timers.pop().delay, 3000);

  context._renderACPPermission(permission);
  assert.strictEqual(output.children.length, 1, 'duplicate permission must not render twice');

  responses.push({ resolved: true });
  context._resolveACPPermission('perm/1', 'reject', output.children[0]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(requests[1].path, '/v1/acp/permissions/perm%2F1');
  assert.strictEqual(requests[1].options.method, 'POST');
  assert.deepStrictEqual(JSON.parse(requests[1].options.body), { decision: 'reject' });

  standalone = false;
  const requestCount = requests.length;
  responses.push({ permissions: [] });
  await context.pollACPPermissions();
  assert.strictEqual(requests.length, requestCount + 1, 'authorized hosted mode must poll permissions');
}

main().then(function() {
  console.log('ACP permission tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});