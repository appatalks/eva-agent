#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/agents/operations.js', 'utf8');
const sandbox = {
  Promise,
  document: {
    addEventListener(type, handler) {
      assert.ok(['DOMContentLoaded', 'keydown'].includes(type));
      this[type] = handler;
    }
  }
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/agents/operations.js' });

const agents = sandbox.EvaAgents;
assert.ok(agents, 'Agent Operations must export EvaAgents');
['open', 'openWorkspace', 'close', 'toggle', 'refresh', 'openAgent', 'invalidateGraph'].forEach((name) => {
  assert.strictEqual(typeof agents[name], 'function', `EvaAgents.${name} must remain available`);
});
['/v1/agents/overview', '/v1/subagent/steer'].forEach((endpoint) => {
  assert.ok(source.includes(endpoint), `${endpoint} contract must remain in Agent Operations`);
});

console.log('agents API tests: PASS');