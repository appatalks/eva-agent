#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const fields = {
  backgroundEnabled: { checked: true },
  backgroundIntervalSeconds: { value: '7200' },
  backgroundSaveButton: { disabled: false },
  backgroundRunNowButton: { disabled: false }
};
const jobs = [
  { checked: true, getAttribute() { return 'memory_consolidation'; } },
  { checked: false, getAttribute() { return 'daily_digest'; } }
];
const requests = [];
let confirmResult = false;
const statuses = [];
const context = {
  AbortSignal,
  Array,
  Date,
  JSON,
  Number,
  Object,
  Promise,
  String,
  encodeURIComponent,
  confirm() { return confirmResult; },
  formatGoalDate(value) { return value || '-'; },
  setStatus(kind, message) { statuses.push({ kind, message }); },
  document: {
    getElementById(id) { return fields[id] || null; },
    querySelectorAll() { return jobs; },
    createElement() { return { appendChild() {}, addEventListener() {}, setAttribute() {}, style: {} }; }
  },
  async backgroundBridgeRequest(path, options) {
    requests.push({ path, options });
    return {};
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/settings/background.js', 'utf8'), context, {
  filename: 'core/js/settings/background.js'
});
context.loadBackgroundData = async function() {};
context.renderBackgroundStatus = function() {};
context.renderBackgroundAll = function() {};

async function main() {
  let controls = context.readBackgroundControls(false);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(controls.body)), {
    enabled: true,
    intervalSeconds: 7200,
    jobs: { memory_consolidation: true, daily_digest: false },
    runNow: false
  });
  controls = context.readBackgroundControls(true);
  assert.strictEqual(controls.body.runNow, true);
  fields.backgroundIntervalSeconds.value = '899';
  assert.strictEqual(context.readBackgroundControls(false).error, 'Interval must be between 900 and 86400 seconds.');
  fields.backgroundIntervalSeconds.value = '86401';
  assert.strictEqual(context.readBackgroundControls(false).error, 'Interval must be between 900 and 86400 seconds.');
  fields.backgroundIntervalSeconds.value = '7200';

  await context.saveBackgroundControls(true);
  assert.strictEqual(requests[0].path, '/v1/background/control');
  assert.strictEqual(requests[0].options.method, 'POST');
  assert.strictEqual(JSON.parse(requests[0].options.body).runNow, true);
  assert.deepStrictEqual(statuses.pop(), { kind: 'info', message: 'Background run queued.' });

  await context.reviewBackgroundProposal('proposal/1', 'approve');
  assert.strictEqual(requests.length, 1, 'declined approval must not call the bridge');
  confirmResult = true;
  await context.reviewBackgroundProposal('proposal/1', 'approve');
  assert.strictEqual(requests[1].path, '/v1/background/proposals/proposal%2F1/approve');
  assert.strictEqual(requests[1].options.method, 'POST');
  await context.reviewBackgroundProposal('proposal/2', 'reject');
  assert.strictEqual(requests[2].path, '/v1/background/proposals/proposal%2F2/reject');
}

main().then(function() {
  console.log('background settings tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});