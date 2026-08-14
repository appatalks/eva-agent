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
  'loadSession', 'toggleSessionPanel', 'initSessions', 'startFreshSessionOnLaunch', 'runEvaTerminalCommand',
  'closeSidePanels', 'closeAgentOperationsForNavigation'
].forEach((name) => {
  assert.strictEqual(typeof sandbox[name], 'function', `${name} must remain globally available`);
});
assert.ok(source.includes('eva_sessions'), 'session index storage key must remain stable');
assert.ok(source.includes('eva_active_session'), 'active session storage key must remain stable');
assert.ok(source.includes('terminalCreate'), 'terminal creation preload contract must remain available');
assert.ok(source.includes("localStorage.setItem(SESSION_PANEL_TAB_KEY, 'chats')"), 'startup must reset Sessions to the chat tab');
assert.ok(source.includes('EvaWorkspaces.closeWorkbench'), 'startup must close a restored workflow view');

const storage = new Map([
  ['eva_active_session', 'sess_workflow'],
  ['aigMessages', JSON.stringify([{ role: 'user', content: 'Run the workflow' }])],
  ['masterOutput', 'Workflow run output'],
]);
const output = { innerHTML: 'Workflow runs' };
const input = { textContent: 'Resume workflow' };
let workbenchClosed = false;
let sidePanelsClosed = false;
let welcomeRestored = false;
sandbox.localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
  removeItem(key) { storage.delete(key); },
};
sandbox.document = {
  getElementById(id) {
    if (id === 'txtOutput') return output;
    if (id === 'txtMsg') return input;
    return null;
  },
};
sandbox.window.EvaWorkspaces = { closeWorkbench() { workbenchClosed = true; } };
sandbox.closeSidePanels = function() { sidePanelsClosed = true; };
sandbox.restoreEvaWelcome = function() { welcomeRestored = true; output.innerHTML = 'Fresh chat'; };
sandbox.startFreshSessionOnLaunch();
assert.strictEqual(workbenchClosed, true, 'startup did not close the retained workflow view');
assert.strictEqual(sidePanelsClosed, true, 'startup did not close retained sidebar views');
assert.strictEqual(welcomeRestored, true, 'startup did not restore the fresh-chat welcome');
assert.strictEqual(output.innerHTML, 'Fresh chat');
assert.strictEqual(input.textContent, '');
assert.strictEqual(storage.has('eva_active_session'), false);
assert.strictEqual(storage.has('aigMessages'), false);
assert.strictEqual(storage.has('masterOutput'), false);
assert.strictEqual(storage.get('eva_session_panel_tab'), 'chats');

console.log('sessions API tests: PASS');