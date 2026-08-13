const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/voice/endpoint.js', 'utf8');
const scheduled = [];
const sandbox = {
  globalThis: {},
  setTimeout: (callback, delay) => {
    const timer = { callback, delay, cancelled: false };
    scheduled.push(timer);
    return timer;
  },
  clearTimeout: timer => { timer.cancelled = true; }
};
vm.runInNewContext(source, sandbox);
const VoiceEndpoint = sandbox.globalThis.VoiceEndpoint;

function latestTimer() {
  return scheduled.filter(timer => !timer.cancelled).slice(-1)[0];
}

const commits = [];
const events = [];
const endpoint = new VoiceEndpoint({
  delayMs: 2200,
  setTimer: sandbox.setTimeout,
  clearTimer: sandbox.clearTimeout,
  onCommit: text => commits.push(text),
  onEvent: event => events.push(event)
});

endpoint.accept('Do you remember the repository that', { provider: 'browser' });
assert.strictEqual(latestTimer().delay, 2200);
endpoint.accept('the repository that we discussed?', { provider: 'browser' });
assert.strictEqual(commits.length, 0, 'short pauses must not submit a partial turn');
latestTimer().callback();
assert.deepStrictEqual(commits, ['Do you remember the repository that we discussed?']);

endpoint.accept('Now generally', { provider: 'browser' });
endpoint.accept('Now generally', { provider: 'browser' });
latestTimer().callback();
assert.strictEqual(commits[1], 'Now generally', 'duplicate final results must be suppressed');
assert(events.some(event => event.type === 'duplicate'));

endpoint.accept('First complete turn', { provider: 'local', delayMs: 600 });
assert.strictEqual(latestTimer().delay, 600);
latestTimer().callback();
endpoint.accept('Second complete turn', { provider: 'local', delayMs: 600 });
latestTimer().callback();
assert.deepStrictEqual(commits.slice(-2), ['First complete turn', 'Second complete turn']);

endpoint.accept('discard me', { provider: 'browser' });
endpoint.reset();
assert.strictEqual(endpoint.flush('stop'), false, 'stopping capture must not submit buffered audio');
assert(events.some(event => event.type === 'interrupted'), 'reset must emit minimized interruption metadata');

endpoint.setDelay(100);
assert.strictEqual(endpoint.delayMs, 1000);
endpoint.setDelay(9000);
assert.strictEqual(endpoint.delayMs, 5000);

console.log('voice endpoint tests passed');