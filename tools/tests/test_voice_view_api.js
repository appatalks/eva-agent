#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const viewSource = fs.readFileSync('core/js/features/voice/view.js', 'utf8');
const endpointSource = fs.readFileSync('core/js/features/voice/endpoint.js', 'utf8');
const optionsSource = fs.readFileSync('core/js/options.js', 'utf8');
const timers = [];
const sandbox = {
  console,
  document: {},
  localStorage: {
    getItem() { return null; },
    setItem() {},
    removeItem() {}
  },
  navigator: {},
  performance: { now() { return 0; } },
  setTimeout(callback, delay) {
    const timer = { callback, delay, cancelled: false };
    timers.push(timer);
    return timer;
  },
  clearTimeout(timer) {
    if (timer) timer.cancelled = true;
  },
  requestAnimationFrame() { return 1; },
  cancelAnimationFrame() {},
  globalThis: null,
  window: null
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;

vm.runInNewContext(endpointSource, sandbox, { filename: 'core/js/features/voice/endpoint.js' });
vm.runInNewContext(viewSource, sandbox, { filename: 'core/js/features/voice/view.js' });

[
  'function applyTheme',
  'function updateButton',
  'async function sendData',
  'function setStatus'
].forEach((signature) => {
  assert.ok(!viewSource.includes(signature), `view.js must not own ${signature}`);
  assert.ok(optionsSource.includes(signature), `options.js must retain ${signature}`);
});

assert.ok(!optionsSource.includes('var _vv ='), 'options.js must not retain the Voice View state owner');
assert.ok(!optionsSource.includes('function openVoiceView()'), 'options.js must not retain the Voice View lifecycle owner');

[
  '_vv',
  'openVoiceView',
  'closeVoiceView',
  'toggleCompactVoiceController',
  '_vvStartListening',
  '_vvStopListening',
  '_vvSendCommand',
  '_vvBargeIn'
].forEach((name) => {
  assert.ok(name in sandbox, `${name} must remain a classic-script global`);
  assert.notStrictEqual(typeof sandbox[name], 'undefined', `${name} must be defined`);
});

assert.strictEqual(sandbox._vv.open, false, 'Voice View must start closed');
assert.strictEqual(typeof sandbox.VoiceEndpoint, 'function', 'VoiceEndpoint must be available before View evaluation');
const endpoint = sandbox._vvGetEndpoint();
assert.ok(endpoint instanceof sandbox.VoiceEndpoint, 'Voice View must construct VoiceEndpoint lazily');
assert.strictEqual(sandbox._vv.endpoint, endpoint, 'constructed endpoint must be retained in _vv state');

console.log('voice view API tests: PASS');