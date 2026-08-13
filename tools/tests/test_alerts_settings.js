#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const fields = {
  alertType: { value: 'keyword_watch' },
  alertLabel: { value: '' },
  alertParamTopic: { value: '', focus() {} },
  alertParamCondition: { value: '' },
  alertCooldown: { value: '24' },
  alertChannelChat: { checked: true },
  alertChannelVoice: { checked: false },
  alertEnabled: { checked: true },
  alertQuietStart: { value: '-1' },
  alertQuietEnd: { value: '-1' },
  alertMaxPerHour: { value: '4' }
};
const requests = [];
const statuses = [];
let confirmResult = false;
const context = {
  Array,
  JSON,
  Math,
  Number,
  Promise,
  String,
  encodeURIComponent,
  confirm() { return confirmResult; },
  escapeHtml(value) { return String(value); },
  formatGoalDate(value) { return value || '-'; },
  setStatus(kind, message) { statuses.push({ kind, message }); },
  document: {
    getElementById(id) { return fields[id] || null; },
    createElement() { return { appendChild() {}, addEventListener() {}, style: {} }; }
  },
  async backgroundBridgeRequest(path, options) {
    requests.push({ path, options });
    return {};
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/settings/alerts.js', 'utf8'), context, {
  filename: 'core/js/settings/alerts.js'
});
context.loadAlerts = async function() {};
context.renderAlertsList = function() {};

async function main() {
  let rule = context.readAlertForm();
  assert.strictEqual(context.alertValidationMessage(rule), 'Topic to watch is required.');
  fields.alertParamTopic.value = ' model releases ';
  fields.alertChannelChat.checked = false;
  rule = context.readAlertForm();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(rule)), {
    type: 'keyword_watch', label: 'Topic watch', params: { topic: 'model releases' },
    cooldown_min: 1440, channels: ['chat'], enabled: true
  });

  fields.alertType.value = 'weather';
  fields.alertParamTopic.value = ' Seattle ';
  fields.alertParamCondition.value = ' rain ';
  fields.alertChannelVoice.checked = true;
  rule = context.readAlertForm();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(rule.params)), { location: 'Seattle', condition: 'rain' });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(rule.channels)), ['voice']);

  await context.saveAlert();
  assert.strictEqual(requests[0].path, '/v1/alerts');
  assert.strictEqual(requests[0].options.method, 'POST');

  await context.deleteAlert('alert/1');
  assert.strictEqual(requests.length, 1, 'declined delete must not call the bridge');
  confirmResult = true;
  await context.deleteAlert('alert/1');
  assert.strictEqual(requests[1].path, '/v1/alerts/alert%2F1');
  assert.strictEqual(requests[1].options.method, 'DELETE');

  fields.alertQuietStart.value = '22';
  fields.alertQuietEnd.value = '7';
  fields.alertMaxPerHour.value = '8';
  await context.saveAlertSettings();
  assert.strictEqual(requests[2].path, '/v1/alerts/settings');
  assert.deepStrictEqual(JSON.parse(requests[2].options.body), {
    quiet_hours_start: 22, quiet_hours_end: 7, max_per_hour: 8
  });
}

main().then(function() {
  console.log('alerts settings tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});