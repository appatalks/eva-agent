#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/automation/browser-agent.js', 'utf8');
const sandbox = { window: {} };
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/automation/browser-agent.js' });

const browser = sandbox.window.EvaBrowser;
const desktop = sandbox.window.EvaDesktop;
assert.ok(browser, 'browser controller must export EvaBrowser');
assert.ok(desktop, 'shared desktop controller must export EvaDesktop');
['launch', 'isActive', 'isAwaitingConfirm', 'answerConfirm', 'close'].forEach((name) => {
  assert.strictEqual(typeof browser[name], 'function', `EvaBrowser.${name} must remain available`);
  assert.strictEqual(typeof desktop[name], 'function', `EvaDesktop.${name} must remain available`);
});
assert.match(source, /endpoint: '\/v1\/browser'/, 'browser endpoint must remain /v1/browser');
assert.match(source, /opts\.endpoint = '\/v1\/desktop'/, 'desktop endpoint must remain /v1/desktop');
assert.match(source, /POST \/v1\/browser\/confirm/, 'confirmation endpoint contract must remain documented');
assert.match(source, /POST \/v1\/browser\/cancel/, 'cancellation endpoint contract must remain documented');

console.log('browser agent API tests: PASS');