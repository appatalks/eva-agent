#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const requests = [];
const statuses = [];
const responses = [];
const context = {
  JSON,
  Promise,
  async detectACPBridge() { return 'http://localhost:8888/'; },
  setStatus(kind, message) { statuses.push({ kind, message }); },
  fetch(url, options) {
    requests.push({ url, options });
    const next = responses.shift();
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(next);
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/features/skills/auto-learn.js', 'utf8'), context, {
  filename: 'core/js/features/skills/auto-learn.js'
});

async function main() {
  responses.push({ ok: true, json: async () => ({ skill: { Name: 'Release notes' } }) });
  const skill = await context.autoLearnSkill([{ role: 'user', content: 'Summarize releases' }], 'Prepare release notes');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(skill)), { Name: 'Release notes' });
  assert.strictEqual(requests[0].url, 'http://localhost:8888/v1/skills/auto-learn');
  assert.strictEqual(requests[0].options.method, 'POST');
  assert.deepStrictEqual(JSON.parse(requests[0].options.body), {
    messages: [{ role: 'user', content: 'Summarize releases' }], task_summary: 'Prepare release notes'
  });
  assert.deepStrictEqual(statuses[0], { kind: 'info', message: 'Skill draft learned: Release notes' });

  responses.push({ ok: true, json: async () => ({}) });
  assert.strictEqual(await context.autoLearnSkill(), null);
  responses.push(new Error('bridge unavailable'));
  assert.strictEqual(await context.autoLearnSkill([], ''), null);
}

main().then(function() {
  console.log('skill auto-learn tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});