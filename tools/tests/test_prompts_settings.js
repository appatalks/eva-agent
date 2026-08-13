#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const fields = {
  selPers: { value: 'default' },
  txtSystemPrompt: {
    value: '',
    listeners: [],
    addEventListener(type, listener) {
      assert.strictEqual(type, 'input');
      this.listeners.push(listener);
    },
    input() {
      this.listeners.forEach((listener) => listener());
    }
  }
};
const storage = new Map();
const context = {
  JSON,
  Object,
  String,
  document: {
    getElementById(id) { return fields[id] || null; }
  },
  localStorage: {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); }
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/settings/prompts.js', 'utf8'), context, {
  filename: 'core/js/settings/prompts.js'
});

const defaultPrompt = context.PERSONALITY_PRESETS.default;
assert.ok(defaultPrompt.includes('You are Eva, a personal AI assistant with persistent memory.'));
fields.txtSystemPrompt.value = '   ';
assert.strictEqual(context.getSystemPrompt(), defaultPrompt, 'blank prompt must fall back to default');
context.initSystemPrompt();

fields.selPers.value = 'advanced';
context.applyPersonalityPreset();
assert.strictEqual(fields.txtSystemPrompt.value, context.PERSONALITY_PRESETS.advanced);
assert.strictEqual(storage.get('systemPrompt'), context.PERSONALITY_PRESETS.advanced);

fields.txtSystemPrompt.value = 'A custom prompt';
storage.set('systemPrompt', 'saved before custom selection');
fields.selPers.value = 'custom';
context.applyPersonalityPreset();
assert.strictEqual(fields.txtSystemPrompt.value, 'A custom prompt', 'custom selection must preserve textarea content');
assert.strictEqual(storage.get('systemPrompt'), 'saved before custom selection', 'custom selection must not overwrite storage');

fields.txtSystemPrompt.value = 'Persist this custom prompt';
fields.txtSystemPrompt.input();
assert.strictEqual(storage.get('systemPrompt'), 'Persist this custom prompt');
assert.strictEqual(fields.selPers.value, 'custom', 'custom input must select the custom preset');

storage.set('systemPrompt', context._STALE_PRESETS.default);
fields.txtSystemPrompt.value = '';
fields.selPers.value = 'custom';
context.initSystemPrompt();
assert.strictEqual(fields.txtSystemPrompt.value, defaultPrompt, 'stale default must migrate to the current preset');
assert.strictEqual(storage.get('systemPrompt'), defaultPrompt, 'migrated prompt must persist');
assert.strictEqual(fields.selPers.value, 'default', 'migrated prompt must select its preset');

context.EvaHarness = { promptContract() { return '\n\n[Harness prompt suffix]'; } };
fields.txtSystemPrompt.value = 'Harness base prompt';
assert.strictEqual(context.getSystemPrompt(), 'Harness base prompt\n\n[Harness prompt suffix]');

console.log('prompts settings tests: PASS');