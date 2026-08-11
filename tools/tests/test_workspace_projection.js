#!/usr/bin/env node
const assert = require('assert');
const { redactKnownPaths } = require('../../standalone/workspace-projection');

const checkout = '/home/eva/.config/eva/worktrees/project/run';
const project = '/home/eva/project';
const report = 'Created ' + checkout + '/script.sh after reading ' + project + '/README.md';
const redacted = redactKnownPaths(report, [project, checkout]);
assert.strictEqual(redacted.includes(checkout), false);
assert.strictEqual(redacted.includes(project), false);
assert.strictEqual(redacted, 'Created <workspace>/script.sh after reading <workspace>/README.md');

const windows = redactKnownPaths('C:\\Eva\\worktrees\\run\\script.ps1', ['C:\\Eva\\worktrees\\run']);
assert.strictEqual(windows, '<workspace>\\script.ps1');
console.log('workspace projection tests: PASS');
