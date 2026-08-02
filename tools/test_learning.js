const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/learning.js', 'utf8');
const sandbox = {
  globalThis: {},
  document: { addEventListener: () => {}, querySelector: () => null },
  localStorage: { getItem: () => null, setItem: () => {} },
  fetch: () => Promise.reject(new Error('not used'))
};
vm.runInNewContext(source, sandbox);
const helpers = sandbox.globalThis.EvaLearning._test;

assert.strictEqual(helpers.mapActionStatus({ status: 'done', result: 'Stopped: user declined the action' }), 'declined');
assert.strictEqual(helpers.mapActionStatus({ status: 'cancelled' }), 'cancelled');
const minimized = helpers.minimizeVoiceEvent({ type: 'merged', provider: 'browser', chars: 19, transcript: 'must not persist' });
assert.strictEqual(JSON.stringify(minimized), JSON.stringify({ type: 'merged', detail: { event: 'merged', provider: 'browser', chars: 19 } }));
assert(!JSON.stringify(minimized).includes('transcript'));

// The browser control is a replace-or-remove operation: same choice removes,
// a different choice replaces the prior signal before creating the new one.
assert.strictEqual(source.includes("prior && prior.status === status ? removeSignal(prior.id)"), true);
assert.strictEqual(source.includes("return sendSignal({"), true);
console.log('learning browser tests passed');