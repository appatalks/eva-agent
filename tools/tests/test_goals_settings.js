#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const fields = {
  goalTitle: { value: '' },
  goalDescription: { value: '' },
  goalCategory: { value: 'relational' },
  goalPriority: { value: '50' },
  goalStatusSelect: { value: 'active' },
  goalEditId: { value: '' }
};

const context = {
  AbortSignal,
  Date,
  JSON,
  Number,
  String,
  confirm() { return true; },
  document: {
    getElementById(id) { return fields[id] || null; },
    createElement() { return { appendChild() {}, addEventListener() {}, style: {} }; }
  },
  setStatus() {}
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/settings/goals.js', 'utf8'), context, {
  filename: 'core/js/settings/goals.js'
});

assert.strictEqual(context.readGoalForm().error, 'Title is required.');

fields.goalTitle.value = '  Improve test coverage  ';
fields.goalDescription.value = '  Add focused regression tests.  ';
fields.goalCategory.value = 'knowledge_curation';
fields.goalPriority.value = '75';
let form = context.readGoalForm();
assert.strictEqual(form.goalId, '');
assert.deepStrictEqual(JSON.parse(JSON.stringify(form.body)), {
  title: 'Improve test coverage',
  description: 'Add focused regression tests.',
  category: 'knowledge_curation',
  priority: 75,
  relatedTopics: ''
});

fields.goalEditId.value = 'goal-123';
fields.goalStatusSelect.value = 'paused';
form = context.readGoalForm();
assert.strictEqual(form.goalId, 'goal-123');
assert.strictEqual(form.body.status, 'paused');

fields.goalPriority.value = '101';
assert.strictEqual(context.readGoalForm().error, 'Priority must be an integer from 0 to 100.');
fields.goalPriority.value = '1.5';
assert.strictEqual(context.readGoalForm().error, 'Priority must be an integer from 0 to 100.');
fields.goalPriority.value = '50';
fields.goalTitle.value = 'x'.repeat(201);
assert.strictEqual(context.readGoalForm().error, 'Title must be 200 characters or fewer.');

console.log('goals settings tests: PASS');