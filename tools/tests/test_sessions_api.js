#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/sessions/explorer.js', 'utf8');
const sandbox = { Promise, window: {} };
sandbox.window.window = sandbox.window;
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/sessions/explorer.js' });

[
  'getAllSessions', 'ensureActiveSessionId', 'saveCurrentSession', 'newSession',
  'loadSession', 'toggleSessionPanel', 'initSessions', 'runEvaTerminalCommand',
  'closeSidePanels', 'closeAgentOperationsForNavigation'
].forEach((name) => {
  assert.strictEqual(typeof sandbox[name], 'function', `${name} must remain globally available`);
});
assert.ok(source.includes('eva_sessions'), 'session index storage key must remain stable');
assert.ok(source.includes('eva_active_session'), 'active session storage key must remain stable');
assert.ok(source.includes('terminalCreate'), 'terminal creation preload contract must remain available');

console.log('sessions API tests: PASS');