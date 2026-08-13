#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/request-routing.js', 'utf8');
const window = {};
vm.runInNewContext(source, { window });
const routing = window.EvaRequestRouting;
assert.ok(routing, 'routing utility must expose EvaRequestRouting');

assert.strictEqual(routing.needsDataRetrieval('What is 2 + 2?'), false);
assert.strictEqual(routing.needsDataRetrieval('Explain how TCP works.'), false);
assert.strictEqual(routing.needsDataRetrieval('What is the current weather?'), true);
assert.strictEqual(routing.needsDataRetrieval('Search the web for the latest NASA news.'), true);
assert.strictEqual(routing.needsDataRetrieval('Search GitHub for the parser.'), true);
assert.strictEqual(routing.needsDataRetrieval('Create a PDF report.'), true);

const lmStudio = fs.readFileSync('core/js/providers/lm-studio.js', 'utf8');
assert.match(lmStudio, /EvaRequestRouting\.needsDataRetrieval/);
assert.match(lmStudio, /if \(_lmsNeedsRetrieval\)/);

const fetchCalls = [];
const sandbox = {
  window,
  EvaRequestRouting: routing,
  fetch: (url) => {
    fetchCalls.push(url);
    return Promise.resolve({ ok: true, json: async () => ({ context: '', cognition_enabled: false }) });
  }
};
vm.runInNewContext(`
  const ordinary = EvaRequestRouting.needsDataRetrieval('Explain recursion.');
  const live = EvaRequestRouting.needsDataRetrieval('What is the weather today?');
  if (ordinary) throw new Error('ordinary explanation was classified as live retrieval');
  if (!live) throw new Error('weather was not classified as live retrieval');
  fetch('http://localhost:8888/v1/memory/context?message=ordinary');
`, sandbox);
assert.strictEqual(fetchCalls.length, 1, 'classifier test should show ordinary path has no retrieval call');
assert.ok(!fetchCalls.some((url) => url.includes('/v1/data/retrieve')));

console.log('request routing tests: PASS');