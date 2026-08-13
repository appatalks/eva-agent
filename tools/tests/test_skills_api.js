#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/skills/library.js', 'utf8');
const sandbox = { Promise, window: {} };
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/skills/library.js' });

const skills = sandbox.window.EvaSkills;
assert.ok(skills, 'Skills library must export EvaSkills');
['open', 'close', 'refresh'].forEach((name) => {
  assert.strictEqual(typeof skills[name], 'function', `EvaSkills.${name} must remain available`);
});
['/v1/skills/evarise', '/v1/skills'].forEach((endpoint) => {
  assert.ok(source.includes(endpoint), `${endpoint} contract must remain in Skills library`);
});
assert.ok(source.includes("method: 'PATCH'"), 'Skills edit lifecycle must retain PATCH');
assert.ok(source.includes("method: 'DELETE'"), 'Skills delete lifecycle must retain DELETE');

console.log('skills API tests: PASS');