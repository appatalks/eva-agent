#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('core/js/sessions.js', 'utf8');
const start = source.indexOf('function evaPlannedTerminalCommandIsSafe');
const end = source.indexOf('\nfunction initTerminal', start);
assert.ok(start >= 0 && end > start);
const plannedResponses = [];
const terminalRuns = [];
const auditEvents = [];
const plannerModels = [];
let selectedPlannerModel = 'gpt-5.6-luna';
let plannerOpenAIKey = 'sk-FAKE-PLANNER-TEST';
const context = {
  AbortSignal,
  document: { getElementById(id) { return id === 'selAIGBackend' ? { value: selectedPlannerModel } : null; } },
  getAuthKey(name) { return name === 'OPENAI_API_KEY' ? plannerOpenAIKey : ''; },
  fetch(_url, options) {
    plannerModels.push(JSON.parse(options.body).model);
    const plannedResponse = plannedResponses.shift();
    if (plannedResponse && typeof plannedResponse === 'object') {
      return Promise.resolve({ ok: false, status: plannedResponse.status, json: () => Promise.resolve(plannedResponse.body || {}) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ choices: [{ message: { content: plannedResponse } }] }) });
  },
  runEvaTerminalCommand(command, submit) {
    terminalRuns.push({ command, submit });
    return Promise.resolve();
  },
  evaAuditEvent(event, outcome, fields) {
    auditEvents.push({ event, outcome, fields });
  },
};
vm.runInNewContext(source.slice(start, end), context);
const safe = context.evaPlannedTerminalCommandIsSafe;

for (const command of ['pwd', 'ls -la', 'df -h', 'git status', 'git diff --stat', 'rg TODO core/js', 'head README.md']) {
  assert.strictEqual(safe(command), true, command);
}
for (const command of ['rm -rf .', 'git status && rm file', 'df /', 'df --output=source,target', 'cat ~/.ssh/id_rsa', 'cat /etc/passwd', 'cat config.local.js', 'head config.local.js', 'rg GITHUB_PAT config.local.js', 'cat .env.local', 'cat .envrc', 'cat .pgpass', 'cat .my.cnf', 'cat .kube/config', 'cat .npmrc', 'cat kubeconfig', 'python -c pass', 'npm test', 'curl example.com', 'git push', 'find . -exec rm {} ;']) {
  assert.strictEqual(safe(command), false, command);
}

assert.match(source, /var shouldSubmit = submit !== false && plannedSafe/);

async function main() {
  plannedResponses.push('{"applicable":false,"command":""}');
  const declined = await context.planEvaTerminalTask('What is your favorite color?', true, true);
  assert.strictEqual(declined.declined, true);
  assert.strictEqual(terminalRuns.length, 0);
  assert.ok(auditEvents.some((item) => item.event === 'terminal_task' && item.outcome === 'completed' && item.fields.action === 'decline'));

  plannedResponses.push('not valid planner JSON');
  const malformed = await context.planEvaTerminalTask('What is your favorite color?', true, true);
  assert.strictEqual(malformed.declined, true);
  assert.strictEqual(terminalRuns.length, 0);

  for (const invalidEnvelope of [
    '[{"applicable":true,"command":"git status"}]',
    '{"applicable":true,"command":["git status"]}',
    '```json\n{"applicable":true,"command":"git status"}\n```',
    '{"applicable":true,"command":"git status","explanation":"safe"}',
    '{"applicable":"true","command":"git status"}',
  ]) {
    plannedResponses.push(invalidEnvelope);
    const invalid = await context.planEvaTerminalTask('Show the current git status.', true, true);
    assert.strictEqual(invalid.declined, true, invalidEnvelope);
    assert.strictEqual(terminalRuns.length, 0, invalidEnvelope);
  }

  plannedResponses.push('{"applicable":true,"command":"npm test"}');
  const modelCountBeforeSuccess = plannerModels.length;
  const staged = await context.planEvaTerminalTask('Can you run the project tests?', true, true);
  assert.strictEqual(staged.submitted, false);
  assert.strictEqual(staged.reviewRequired, true);
  assert.deepStrictEqual(terminalRuns[0], { command: 'npm test', submit: false });
  assert.deepStrictEqual(plannerModels.slice(modelCountBeforeSuccess), ['gpt-5.6-luna']);

  const modelCountBeforeRetry = plannerModels.length;
  plannedResponses.push(
    { status: 410, body: { error: { message: 'Selected planner unavailable.' } } },
    '{"applicable":true,"command":"npm test"}'
  );
  const retried = await context.planEvaTerminalTask('Can you run the project tests?', true, true);
  assert.strictEqual(retried.submitted, false);
  assert.strictEqual(retried.reviewRequired, true);
  assert.deepStrictEqual(plannerModels.slice(modelCountBeforeRetry), ['gpt-5.6-luna', 'openai:gpt-5-mini']);
  assert.deepStrictEqual(terminalRuns[1], { command: 'npm test', submit: false });

  selectedPlannerModel = 'openai:gpt-5';
  const modelCountBeforeOpenAI = plannerModels.length;
  plannedResponses.push({ status: 503, body: { error: { message: 'OpenAI planner unavailable.' } } });
  await assert.rejects(context.planEvaTerminalTask('Check the workspace.', true, true), /OpenAI planner unavailable/);
  assert.deepStrictEqual(plannerModels.slice(modelCountBeforeOpenAI), ['openai:gpt-5']);

  selectedPlannerModel = 'gpt-5.6-luna';
  plannerOpenAIKey = '';
  const modelCountBeforeNoKey = plannerModels.length;
  plannedResponses.push({ status: 410, body: { error: { message: 'Selected planner unavailable.' } } });
  await assert.rejects(context.planEvaTerminalTask('Check the workspace.', true, true), /Selected planner unavailable/);
  assert.deepStrictEqual(plannerModels.slice(modelCountBeforeNoKey), ['gpt-5.6-luna']);

  console.log('terminal command policy tests: PASS');
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});