#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const stored = new Map([
  ['cogReviewerModel', 'gpt-5.6-terra'],
]);
let backendModel = 'openai:gpt-5';

const context = {
  console,
  setTimeout,
  clearTimeout,
  document: {
    getElementById(id) {
      return id === 'selAIGBackend' ? { value: backendModel } : null;
    },
  },
  localStorage: {
    getItem(key) {
      return stored.has(key) ? stored.get(key) : null;
    },
    setItem(key, value) {
      stored.set(key, String(value));
    },
    removeItem(key) {
      stored.delete(key);
    },
  },
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/cognition.js', 'utf8'), context, {
  filename: 'core/js/cognition.js',
});

const directConfig = context.Cognition.getCfg();
assert.strictEqual(directConfig.evaModel, 'openai:gpt-5');
assert.strictEqual(directConfig.reviewerModel, 'openai:gpt-5.6-luna');
assert.strictEqual(stored.get('cogReviewerModel'), 'openai:gpt-5.6-luna');

backendModel = 'gpt-5.6-luna';
context.Cognition.setCfg({ reviewerModel: 'gpt-5.6-terra' });
const acpConfig = context.Cognition.getCfg();
assert.strictEqual(acpConfig.evaModel, 'gpt-5.6-luna');
assert.strictEqual(acpConfig.reviewerModel, 'gpt-5.6-terra');

console.log('cognition provider tests: PASS');
