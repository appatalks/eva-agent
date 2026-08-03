#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

async function main() {
  const optionsSource = fs.readFileSync('core/js/options.js', 'utf8');
  const helperStart = optionsSource.indexOf('function normalizeModelMaxTokens');
  const helperEnd = optionsSource.indexOf('function getModelMaxTokens', helperStart);
  assert(helperStart >= 0 && helperEnd > helperStart);
  const helperContext = {};
  vm.runInNewContext(optionsSource.slice(helperStart, helperEnd), helperContext);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('32768'), 32768);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('1e2'), null);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('1.0'), null);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('128001'), 128000);

  const storage = new Map();
  const txtMsg = { innerHTML: 'hello Eva', focus() {} };
  const txtOutput = { innerHTML: '', innerText: '', scrollTop: 0, scrollHeight: 0 };
  const autoSpeak = { checked: false };
  let capturedPayload = null;
  let completionStatus = null;
  let resolveRequest;
  const requestCaptured = new Promise((resolve) => { resolveRequest = resolve; });

  const context = {
    console,
    JSON,
    Promise,
    AbortSignal,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    txtMsg,
    lastResponse: '',
    dateContents: '',
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
    },
    document: {
      getElementById(id) {
        if (id === 'txtMsg') return txtMsg;
        if (id === 'txtOutput') return txtOutput;
        if (id === 'autoSpeak') return autoSpeak;
        return null;
      },
    },
    EvaPromptBudget: {
      compactMessages(messages) { return { messages }; },
    },
    getSystemPrompt() { return 'Eva test prompt'; },
    getACPBridgeUrl() { return 'http://localhost:8888'; },
    getLmStudioBaseUrl() { return 'http://localhost:1234/v1'; },
    getLmStudioModel() { return 'test-local-model'; },
    getModelMaxTokens() { return 32768; },
    async renderEvaResponse() {},
    reportCompletionTruncation(data) {
      if (data.choices[0].finish_reason === 'length') completionStatus = 'warn';
      return completionStatus === 'warn';
    },
    fetch(url, options) {
      if (url.endsWith('/v1/memory/reflect')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) });
      }
      if (url.includes('/v1/memory/context')) {
        return Promise.resolve({ ok: true, json: async () => ({ context: '', cognition_enabled: false }) });
      }
      if (url.endsWith('/chat/completions')) {
        capturedPayload = JSON.parse(options.body);
        resolveRequest();
        return Promise.resolve({
          ok: true,
          json: async () => ({
            choices: [{ message: { content: 'local response' }, finish_reason: 'length' }],
          }),
        });
      }
      return Promise.reject(new Error('unexpected URL: ' + url));
    },
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync('core/js/lm-studio.js', 'utf8'), context, {
    filename: 'core/js/lm-studio.js',
  });

  context.lmsSend();
  await requestCaptured;
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.strictEqual(capturedPayload.max_tokens, 32768);
  assert.strictEqual(capturedPayload.model, 'test-local-model');
  assert.strictEqual(completionStatus, 'warn');
  console.log('provider token budget tests: PASS');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
