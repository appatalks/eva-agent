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
assert.strictEqual(routing.classifyRequestType('List open GitHub issues for owner/repository.'), 'github-data');
assert.strictEqual(routing.classifyRequestType('Submit an issue to https://github.com/example/eva-agent/issues.'), 'github-data');
assert.strictEqual(routing.isGitHubOperation('Review GitHub pull requests for owner/repository.'), true);
assert.strictEqual(routing.isGitHubOperation('Explain GitHub merge conflicts.'), false);
assert.strictEqual(routing.classifyRequestType('Send a test email to peer@example.com.'), 'email-action');
assert.strictEqual(routing.classifyRequestType('Please try sending a test email to peer@example.com.'), 'email-action');
assert.strictEqual(routing.classifyRequestType('How does email work?'), 'general');
assert.strictEqual(routing.needsDataRetrieval('Create a PDF report.'), true);
assert.strictEqual(routing.isNativeWeatherLookup('What is the weather in Seattle?'), true);
assert.strictEqual(routing.isNativeWeatherLookup('Use the browser to check the weather in Seattle.'), false);
assert.strictEqual(routing.isNativeWeatherLookup('Check the weather in my browser.'), false);
assert.strictEqual(routing.isExplicitInteractiveRequest('Open the weather website.'), true);
assert.strictEqual(routing.isExplicitInteractiveRequest('Check the weather in my browser.'), true);
assert.strictEqual(routing.isExplicitCameraRequest('Look through the webcam and describe the room.'), true);
assert.strictEqual(routing.isExplicitCameraRequest('Confirmed. Please send the email.'), false);
assert.strictEqual(routing.isExplicitCameraRequest('Do not use the camera.'), false);
assert.strictEqual(routing.isExplicitCameraRequest('What is a camera shutter?'), false);
assert.strictEqual(routing.isExplicitCameraRequest('The camera is unavailable.'), false);
assert.strictEqual(routing.isExplicitCameraRequest('What can you see?'), false);
assert.strictEqual(routing.isExplicitCameraRequest('What am I holding?'), false);
assert.strictEqual(routing.isNarrowNativeOperation('Send a test email to peer@example.com.'), true);
assert.strictEqual(routing.isNarrowNativeOperation('Create a CSV report.'), true);
assert.strictEqual(routing.needsDataRetrieval('Create a DOCX report.'), true);
assert.strictEqual(routing.isNarrowNativeOperation('Validate this XLSX workbook.'), true);
assert.strictEqual(routing.isNarrowNativeOperation('Scaffold a FastMCP server.'), true);
assert.strictEqual(routing.isNarrowNativeOperation('Use the desktop app to check this CSV.'), false);

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