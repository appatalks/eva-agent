#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const fields = {
  cronLabel: { value: '' },
  cronSchedule: { value: '' },
  cronPrompt: { value: '' },
  cronStatus: { textContent: '' }
};
const requests = [];
const responses = [];
const context = {
  JSON,
  Promise,
  encodeURIComponent,
  document: {
    getElementById(id) { return fields[id] || null; },
    createElement() { return { appendChild() {}, innerHTML: '' }; },
    createTextNode(value) { return { value }; }
  },
  async detectACPBridge() { return 'http://localhost:8888/'; },
  fetch(url, options) {
    requests.push({ url, options });
    return Promise.resolve(responses.shift() || { ok: true, json: async () => ({}) });
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/settings/cron.js', 'utf8'), context, {
  filename: 'core/js/settings/cron.js'
});
context.cronRefresh = async function() {};

async function main() {
  await context.cronAdd();
  assert.strictEqual(fields.cronStatus.textContent, 'All three fields are required.');
  assert.strictEqual(requests.length, 0);

  fields.cronLabel.value = ' Morning briefing ';
  fields.cronSchedule.value = ' 0 8 * * 1-5 ';
  fields.cronPrompt.value = ' Prepare the briefing ';
  responses.push({ ok: true, json: async () => ({ task: { label: 'Morning briefing' } }) });
  await context.cronAdd();
  assert.strictEqual(requests[0].url, 'http://localhost:8888/v1/cron');
  assert.strictEqual(requests[0].options.method, 'POST');
  assert.deepStrictEqual(JSON.parse(requests[0].options.body), {
    label: 'Morning briefing', schedule: '0 8 * * 1-5', prompt: 'Prepare the briefing'
  });
  assert.strictEqual(fields.cronLabel.value, '');
  assert.strictEqual(fields.cronSchedule.value, '');
  assert.strictEqual(fields.cronPrompt.value, '');

  await context.cronToggle('task/1', false);
  assert.strictEqual(requests[1].url, 'http://localhost:8888/v1/cron/task%2F1');
  assert.strictEqual(requests[1].options.method, 'PATCH');
  assert.deepStrictEqual(JSON.parse(requests[1].options.body), { enabled: false });

  await context.cronDelete('task/1');
  assert.strictEqual(requests[2].url, 'http://localhost:8888/v1/cron/task%2F1');
  assert.strictEqual(requests[2].options.method, 'DELETE');
}

main().then(function() {
  console.log('cron settings tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});