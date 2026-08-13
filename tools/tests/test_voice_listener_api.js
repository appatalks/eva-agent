#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/features/voice/wake-listener.js', 'utf8');
const sandbox = { console, window: {} };
vm.runInNewContext(source, sandbox, { filename: 'core/js/features/voice/wake-listener.js' });

[
  'startVoiceListener', 'stopVoiceListener', 'startSpeechRecognition',
  '_handleVoiceTranscript', '_sendVoiceCommand', '_runVoiceNavigationCommand', '_setMicStatus'
].forEach((name) => {
  assert.strictEqual(typeof sandbox[name], 'function', `${name} must remain globally available`);
});
assert.ok(source.includes("lower.indexOf('eva')"), 'Eva wake-word matcher must remain explicit');
assert.ok(source.includes('recordConversationTurn'), 'spoken conversation persistence must remain wired');

console.log('voice listener API tests: PASS');