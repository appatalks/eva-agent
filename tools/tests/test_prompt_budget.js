#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/prompt-budget.js', 'utf8');
const window = {};
vm.runInNewContext(source, { window });
const budget = window.EvaPromptBudget;
assert.ok(budget, 'utility must expose EvaPromptBudget');

const history = [
  { role: 'system', content: 'PINNED SYSTEM' },
  { role: 'developer', content: 'PINNED DEVELOPER' },
  { role: 'system', content: 'PINNED SYSTEM' },
  { role: 'user', content: 'Old request about the deployment' },
  { role: 'assistant', content: '[Action outcome] deployment completed successfully' },
  { role: 'user', content: 'Actually, use the staging environment instead.' },
  { role: 'assistant', content: 'The staging task is still pending.' },
  { role: 'user', content: 'Recent request one' },
  { role: 'assistant', content: 'Recent answer one' },
  { role: 'user', content: 'Recent request two' },
  { role: 'assistant', content: 'Recent answer two' }
];
const snapshot = JSON.stringify(history);
const packed = budget.compactMessages(history, { budget: 900, recentTurns: 2, summaryChars: 500 });
const texts = packed.messages.map(budget.textOf);

assert.strictEqual(JSON.stringify(history), snapshot, 'compaction must not mutate persistent history');
assert.strictEqual(packed.dedupedMessages, 1, 'exact repeated static messages are deduplicated');
assert.ok(texts.includes('PINNED SYSTEM'), 'system instructions remain pinned');
assert.ok(texts.includes('PINNED DEVELOPER'), 'developer instructions remain pinned');
assert.ok(texts.includes('Recent request one') && texts.includes('Recent answer two'), 'recent turns are retained');
assert.ok(!texts.includes('Old request about the deployment'), 'old conversation is compacted out of the tail');
assert.match(packed.summary, /Earlier conversation:/, 'dropped conversation gets a bounded summary');
assert.match(packed.summary, /Action outcomes:/, 'action outcomes carry forward');
assert.match(packed.summary, /Corrections:/, 'corrections carry forward');
assert.match(packed.summary, /Open task state:/, 'unresolved task state carries forward');
assert.ok(packed.components.pinned.tokens > 0 && packed.components.recent.tokens > 0, 'component token estimates are present');
assert.ok(packed.estimatedTokens <= 900, 'request view stays within the budget');

const tight = budget.compactMessages([
  { role: 'user', content: 'u'.repeat(2000) }
], { budget: 256, recentTurns: 1 });
assert.ok(tight.estimatedTokens <= 256, 'single-message clipping must honor the exact budget');

const carried = budget.compactMessages([
  { role: 'system', content: 'PINNED SYSTEM' },
  { role: 'summary', content: '[Conversation Summary]\nEarlier conversation: old context' },
  { role: 'user', content: 'New current request' }
], { budget: 900, recentTurns: 1 });
assert.match(carried.summary, /Prior summary:/, 'prior summary is carried forward');
assert.match(carried.summary, /old context/, 'prior summary content is retained');

const gemini = budget.compactGeminiContents([
  { role: 'user', parts: [{ text: 'Gemini system instruction' }] },
  { role: 'model', parts: [{ text: 'Previous answer' }] },
  { role: 'user', parts: [{ text: 'Current question' }] }
], { budget: 900, recentTurns: 1, pinnedIndexes: [0] });
assert.strictEqual(gemini.messages[0].role, 'user', 'Gemini pinned instruction keeps user role shape');
assert.strictEqual(gemini.messages[1].role, 'model', 'Gemini assistant role is restored as model');
assert.ok(gemini.messages.some((message) => budget.textOf(message) === 'Current question'));

console.log('prompt budget tests: PASS');
