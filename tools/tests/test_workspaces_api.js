#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/workspaces/monitor.js', 'utf8');
const sandbox = {
  Promise,
  setInterval() { return 1; },
  clearInterval() {},
  document: {
    addEventListener(type, handler) {
      assert.strictEqual(type, 'DOMContentLoaded');
      this.ready = handler;
    }
  }
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/workspaces/monitor.js' });

const workspaces = sandbox.EvaWorkspaces;
assert.ok(workspaces, 'Workspace Monitor must export EvaWorkspaces');
[
  'toggle', 'refresh', 'describe', 'describeProjectTools', 'mcpContext', 'openWorkbench',
  'closeWorkbench', 'open', 'importGitHub', 'listGitHubRepositories',
  'continueGitHubRepositories', 'authorizeGitHub', 'removeProjectByName',
  'currentProjectId',
  'setProjectMcpServerByName', 'verifyProjectMcpServerByName', 'runSelectedCheck',
  'importGitHubSelection', 'promptGitHubImport'
].forEach((name) => {
  assert.strictEqual(typeof workspaces[name], 'function', `EvaWorkspaces.${name} must remain available`);
});
assert.strictEqual(typeof sandbox.toggleWorkspacePanel, 'function', 'legacy Workspace toggle must remain global');
assert.ok(source.includes("WORKSPACE_DISPLAY_STATE_STORAGE_KEY = 'eva.workspaceMonitorDisplay.v1'"),
  'Workspace display clear state must use a durable localStorage key');
assert.ok(source.includes('function workspaceDisplayState()') && source.includes('function persistWorkspaceDisplayState()'),
  'Workspace display clear state must be restored and persisted');
assert.ok(source.includes("localStorage.getItem('workspaceAutoApprove') !== 'false'"),
  'Workspace auto-approval must default to enabled unless explicitly disabled');
assert.ok(source.includes('clearedActivityProjectIds: savedDisplayState.clearedActivityProjectIds'),
  'Activity display clears must survive renderer reload');
assert.ok(source.includes('clearedCodingRunProjectIds: savedDisplayState.clearedCodingRunProjectIds'),
  'Coding run display clears must survive renderer reload');
assert.ok(source.includes('clearedResultRunIds: savedDisplayState.clearedResultRunIds'),
  'Run result display clears must survive renderer reload');
assert.ok(source.includes('function pruneWorkspaceDisplayState()') && source.includes('pruneWorkspaceDisplayState();'),
  'Deleted workspace and run IDs must be pruned from saved display state');
assert.ok(source.includes("selected.status === 'active' && (!selected.agent || selected.agent.status === 'error')"),
  'Workspace runs delayed before agent creation must expose the retry control');
assert.ok(source.includes('monitorRunStatesInitialized: false') && source.includes('if (!state.monitorRunStatesInitialized)'),
  'The initial workspace monitor snapshot must not narrate retained runs into a fresh chat');

console.log('workspaces API tests: PASS');