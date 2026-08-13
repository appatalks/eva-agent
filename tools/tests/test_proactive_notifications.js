#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const requests = [];
const responses = [];
const spoken = [];
const output = { innerHTML: '', scrollTop: 0, scrollHeight: 20 };
const context = {
  AbortSignal,
  Array,
  JSON,
  Promise,
  String,
  clearInterval,
  clearTimeout,
  setInterval() { return 11; },
  setTimeout() { return 12; },
  escapeHtml(value) { return String(value).replace(/</g, '&lt;'); },
  hideEvaWelcome() {},
  speakText(value) { spoken.push(value); },
  document: { getElementById(id) { return id === 'txtOutput' ? output : null; } },
  async backgroundBridgeRequest(path, options) {
    requests.push({ path, options });
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return next;
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/features/notifications/proactive.js', 'utf8'), context, {
  filename: 'core/js/features/notifications/proactive.js'
});

async function main() {
  responses.push({ notifications: [
    { id: 'n1', title: 'Weather', body: 'Rain <soon>', channels: ['chat', 'voice'] },
    { id: 'n2', title: 'News', body: 'Update', channels: ['voice'] }
  ] });
  responses.push({ seen: 2 });
  await context.pollNotifications();
  assert.strictEqual(requests[0].path, '/v1/notifications?unseen_only=1&limit=10');
  assert.strictEqual(requests[0].options.method, 'GET');
  assert.strictEqual(requests[1].path, '/v1/notifications/seen');
  assert.deepStrictEqual(JSON.parse(requests[1].options.body), { ids: ['n1', 'n2'] });
  assert.strictEqual(spoken[0], 'Weather. Rain <soon>. News. Update');
  assert.match(output.innerHTML, /Rain &lt;soon>/);
  assert.strictEqual(context._notifState.polling, false);

  responses.push({ notifications: [] });
  await context.pollNotifications();
  assert.strictEqual(requests.length, 3, 'empty result must not send seen acknowledgment');

  responses.push(new Error('bridge unavailable'));
  await context.pollNotifications();
  assert.strictEqual(context._notifState.polling, false, 'transport failure must release polling gate');

  context.initNotifications();
  assert.strictEqual(context._notifState.timer, 11);
}

main().then(function() {
  console.log('proactive notification tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});