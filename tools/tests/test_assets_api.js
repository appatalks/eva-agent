#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/assets/library.js', 'utf8');
const sandbox = {
  Promise,
  window: {},
  document: {
    addEventListener(type, handler) {
      assert.strictEqual(type, 'DOMContentLoaded');
      this.ready = handler;
    }
  }
};
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/assets/library.js' });

const assets = sandbox.EvaAssets;
assert.ok(assets, 'Assets controller must export EvaAssets');
['open', 'close', 'refresh'].forEach((name) => {
  assert.strictEqual(typeof assets[name], 'function', `EvaAssets.${name} must remain available`);
});
assert.ok(source.includes("'/v1/files'"), 'generated Assets endpoint must remain /v1/files');
assert.ok(source.includes('workspaceListAssets'), 'workspace Assets integration must remain available');
assert.ok(source.includes('workspaceOpenAsset'), 'workspace file opening must remain delegated to Electron');

console.log('assets API tests: PASS');