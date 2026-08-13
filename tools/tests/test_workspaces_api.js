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
  'setProjectMcpServerByName', 'verifyProjectMcpServerByName', 'runSelectedCheck',
  'importGitHubSelection', 'promptGitHubImport'
].forEach((name) => {
  assert.strictEqual(typeof workspaces[name], 'function', `EvaWorkspaces.${name} must remain available`);
});
assert.strictEqual(typeof sandbox.toggleWorkspacePanel, 'function', 'legacy Workspace toggle must remain global');

console.log('workspaces API tests: PASS');