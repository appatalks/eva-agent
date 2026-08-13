#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

async function main() {
  const modelSettingsSource = fs.readFileSync('core/js/settings/model-settings.js', 'utf8');
  const helperStart = modelSettingsSource.indexOf('function normalizeModelMaxTokens');
  const helperEnd = modelSettingsSource.indexOf('function getModelMaxTokens', helperStart);
  assert(helperStart >= 0 && helperEnd > helperStart);
  const helperContext = {};
  vm.runInNewContext(modelSettingsSource.slice(helperStart, helperEnd), helperContext);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('32768'), 32768);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('1e2'), null);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('1.0'), null);
  assert.strictEqual(helperContext.normalizeModelMaxTokens('128001'), 128000);

  const storage = new Map();
  const txtMsg = {
    innerHTML: '<scr<script>ipt>alert(1)</scr<script>ipt>',
    innerText: 'hello Eva',
    textContent: 'hello Eva',
    focus() {},
  };
  const txtOutput = { innerHTML: '', innerText: '', scrollTop: 0, scrollHeight: 0 };
  const autoSpeak = { checked: false };
  let capturedPayload = null;
  let memoryContextUrl = null;
  let reflectionPayload = null;
  let sessionCalls = 0;
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
    ensureActiveSessionId() {
      sessionCalls += 1;
      return sessionCalls === 1 ? 'session-provider' : 'session-switched';
    },
    async renderEvaResponse() {},
    reportCompletionTruncation(data) {
      if (data.choices[0].finish_reason === 'length') completionStatus = 'warn';
      return completionStatus === 'warn';
    },
    fetch(url, options) {
      if (url.endsWith('/v1/memory/reflect')) {
        reflectionPayload = JSON.parse(options.body);
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) });
      }
      if (url.includes('/v1/memory/context')) {
        memoryContextUrl = url;
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
  vm.runInNewContext(fs.readFileSync('core/js/providers/lm-studio.js', 'utf8'), context, {
    filename: 'core/js/providers/lm-studio.js',
  });

  context.lmsSend();
  await requestCaptured;
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.strictEqual(capturedPayload.max_tokens, 32768);
  assert.strictEqual(capturedPayload.model, 'test-local-model');
  assert.strictEqual(capturedPayload.messages.at(-1).content, 'hello Eva');
  assert.ok(!JSON.stringify(capturedPayload).includes('<script'));
  assert.ok(memoryContextUrl.includes('session_id=session-provider'));
  assert.strictEqual(reflectionPayload.session_id, 'session-provider');
  assert.strictEqual(sessionCalls, 1);
  assert.strictEqual(completionStatus, 'warn');
  await testGeminiMemoryLifecycle();
  await testGeminiMemoryLifecycle('abc', 'u'.repeat(2000));
  await testGeminiMemoryLifecycle('s'.repeat(40000), 'u'.repeat(2000));
  await testOpenAITurnSessionRetention();
  await testGitHubModelsTurnSessionRetention();
  testAcpReflectionOwnership();
  console.log('provider token budget tests: PASS');
}

async function testOpenAITurnSessionRetention() {
  const storage = new Map();
  const txtMsg = { innerHTML: 'openai turn', focus() {} };
  const txtOutput = { innerHTML: '', innerText: '', scrollTop: 0, scrollHeight: 0 };
  const selModel = { value: 'gpt-4o' };
  let contextUrl = '';
  let reflectionPayload = null;
  let sessionCalls = 0;
  let resolveReflection;
  const reflectionCaptured = new Promise((resolve) => { resolveReflection = resolve; });

  class MockXMLHttpRequest {
    open() {}
    setRequestHeader() {}
    send() {
      this.readyState = 4;
      this.status = 200;
      this.responseText = JSON.stringify({
        choices: [{ message: { content: 'openai response' } }],
        usage: { completion_tokens: 1, total_tokens: 2 },
      });
      setImmediate(() => this.onreadystatechange());
    }
  }

  const context = {
    AbortSignal,
    JSON,
    Promise,
    XMLHttpRequest: MockXMLHttpRequest,
    console,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    txtMsg,
    txtOutput,
    selModel,
    OPENAI_API_KEY: 'test-key',
    retryCount: 0,
    maxRetries: 0,
    lastResponse: '',
    masterOutput: '',
    imgSrcGlobal: '',
    dateContents: '',
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
    },
    document: {
      getElementById(id) {
        if (id === 'txtMsg') return txtMsg;
        if (id === 'txtOutput') return txtOutput;
        if (id === 'selModel') return selModel;
        if (id === 'autoSpeak') return { checked: false };
        return null;
      },
      createElement() { return { appendChild() {} }; },
    },
    EvaPromptBudget: { compactMessages(messages) { return { messages }; } },
    getSystemPrompt() { return 'Eva OpenAI prompt'; },
    getACPBridgeUrl() { return 'http://localhost:8888'; },
    getModelMaxTokens() { return 1024; },
    getModelTemperature() { return 0.7; },
    ensureActiveSessionId() {
      sessionCalls += 1;
      return sessionCalls === 1 ? 'openai-session' : 'switched-session';
    },
    async renderEvaResponse() {},
    reportCompletionTruncation() { return false; },
    escapeHtml(value) { return String(value); },
    fetch(url, options) {
      if (url.includes('/v1/memory/context')) {
        contextUrl = url;
        return Promise.resolve({ ok: true, json: async () => ({ context: '', cognition_enabled: false }) });
      }
      if (url.endsWith('/v1/memory/reflect')) {
        reflectionPayload = JSON.parse(options.body);
        resolveReflection();
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) });
      }
      return Promise.reject(new Error('unexpected URL: ' + url));
    },
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync('core/js/providers/openai.js', 'utf8'), context, {
    filename: 'core/js/providers/openai.js',
  });

  context.trboSend();
  await reflectionCaptured;

  assert.ok(contextUrl.includes('session_id=openai-session'));
  assert.strictEqual(reflectionPayload.session_id, 'openai-session');
  assert.strictEqual(sessionCalls, 1);
}

async function testGitHubModelsTurnSessionRetention() {
  const storage = new Map();
  const txtOutput = { innerHTML: '', innerText: '', scrollTop: 0, scrollHeight: 0 };
  let contextUrl = '';
  let reflectionPayload = null;
  let sessionCalls = 0;
  let resolveReflection;
  const reflectionCaptured = new Promise((resolve) => { resolveReflection = resolve; });
  const context = {
    AbortSignal,
    JSON,
    Promise,
    console,
    URL,
    encodeURIComponent,
    txtOutput,
    lastResponse: '',
    masterOutput: '',
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
    },
    document: {
      getElementById(id) {
        if (id === 'autoSpeak') return { checked: false };
        return null;
      },
      addEventListener() {},
    },
    EvaPromptBudget: { compactMessages(messages) { return { messages }; } },
    getAuthKey() { return 'github-token'; },
    getModelTemperature() { return 0.7; },
    getModelMaxTokens() { return 1024; },
    getACPBridgeUrl() { return 'http://localhost:8888'; },
    ensureActiveSessionId() {
      sessionCalls += 1;
      return sessionCalls === 1 ? 'github-session' : 'switched-session';
    },
    async renderEvaResponse() {},
    reportCompletionTruncation() { return false; },
    setStatus() {},
    fetch(url, options) {
      if (url.includes('/v1/memory/context')) {
        contextUrl = url;
        return Promise.resolve({ ok: true, json: async () => ({ context: '', cognition_enabled: false }) });
      }
      if (url.includes('models.github.ai')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ choices: [{ message: { content: 'github response' } }] }),
        });
      }
      if (url.endsWith('/v1/memory/reflect')) {
        reflectionPayload = JSON.parse(options.body);
        resolveReflection();
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) });
      }
      return Promise.reject(new Error('unexpected URL: ' + url));
    },
  };
  vm.runInNewContext(fs.readFileSync('core/js/providers/copilot.js', 'utf8'), context, {
    filename: 'core/js/providers/copilot.js',
  });

  await context._copilotSendModelsAPI(
    [{ role: 'system', content: 'Eva GitHub prompt' }, { role: 'user', content: 'github turn' }],
    'copilot-gpt-4o',
    'github turn',
    txtOutput,
    'copilotMessages',
    null,
  );
  await reflectionCaptured;

  assert.ok(contextUrl.includes('session_id=github-session'));
  assert.strictEqual(reflectionPayload.session_id, 'github-session');
  assert.strictEqual(sessionCalls, 1);
}

async function testGeminiMemoryLifecycle(configuredSystemPrompt, userPrompt) {
  const storage = new Map();
  const requestText = userPrompt || 'remember this project';
  const txtMsg = {
    innerHTML: '<scr<script>ipt>alert(1)</scr<script>ipt>',
    innerText: requestText,
    textContent: requestText,
    focus() {},
  };
  const txtOutput = { innerHTML: '', innerText: '', scrollTop: 0, scrollHeight: 0 };
  let contextUrl = '';
  let geminiPayload = null;
  let reflectionPayload = null;
  let resolveReflection;
  const reflectionCaptured = new Promise((resolve) => { resolveReflection = resolve; });
  const budgetSandbox = { window: {} };
  vm.runInNewContext(fs.readFileSync('core/js/prompt-budget.js', 'utf8'), budgetSandbox, {
    filename: 'core/js/prompt-budget.js',
  });
  const geminiBudget = budgetSandbox.window.EvaPromptBudget;
  const context = {
    AbortSignal,
    JSON,
    Promise,
    console,
    encodeURIComponent,
    txtMsg,
    dateContents: '',
    window: { __LOCAL_CONFIG__: { GOOGLE_GL_KEY: 'test-key' } },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
    },
    document: {
      getElementById(id) {
        if (id === 'txtMsg') return txtMsg;
        if (id === 'txtOutput') return txtOutput;
        return null;
      },
    },
    EvaPromptBudget: geminiBudget,
    getSystemPrompt() { return configuredSystemPrompt || 'Eva Gemini prompt'; },
    getACPBridgeUrl() { return 'http://localhost:8888'; },
    ensureActiveSessionId() { return 'gemini-session'; },
    escapeHtml(value) { return String(value); },
    async renderEvaResponse() {},
    fetch(url, options) {
      if (url.includes('/v1/memory/context')) {
        contextUrl = url;
        return Promise.resolve({ ok: true, json: async () => ({ context: '[Core Memory] ' + 'm'.repeat(40000), cognition_enabled: true }) });
      }
      if (new URL(url).hostname === 'generativelanguage.googleapis.com') {
        geminiPayload = JSON.parse(options.body);
        return Promise.resolve({
          ok: true,
          json: async () => ({
            candidates: [{ finishReason: 'STOP', content: { parts: [{ text: 'Gemini memory response' }] } }],
          }),
        });
      }
      if (url.endsWith('/v1/memory/reflect')) {
        reflectionPayload = JSON.parse(options.body);
        resolveReflection();
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) });
      }
      return Promise.reject(new Error('unexpected URL: ' + url));
    },
  };
  vm.runInNewContext(fs.readFileSync('core/js/providers/gemini.js', 'utf8'), context, {
    filename: 'core/js/providers/gemini.js',
  });

  context.geminiSend();
  await reflectionCaptured;

  assert.ok(contextUrl.includes('session_id=gemini-session'));
  assert.ok(!JSON.stringify(geminiPayload).includes('<script'));
  if (!configuredSystemPrompt) {
    assert.ok(geminiPayload.systemInstruction.parts[0].text.startsWith('[Core Memory]'));
  } else if (configuredSystemPrompt.length <= 1000) {
    const seedOccurrences = geminiPayload.systemInstruction.parts[0].text.split(configuredSystemPrompt).length - 1;
    assert.strictEqual(seedOccurrences, 1, 'configured Gemini system seed must remain exactly once under memory pressure');
  }
  const aggregateTokens = geminiBudget.estimateTokens(geminiPayload.systemInstruction.parts[0].text) +
    geminiPayload.contents.reduce((total, message) => total + geminiBudget.estimateTokens(geminiBudget.textOf(message)), 0);
  assert.ok(aggregateTokens <= 10000, 'Gemini contents and system instruction must share the request budget');
  assert.ok(!geminiPayload.contents.some((message) => geminiBudget.textOf(message) === geminiBudget.textOf(geminiPayload.systemInstruction)), 'Gemini system instruction must not be duplicated in contents');
  assert.strictEqual(reflectionPayload.session_id, 'gemini-session');
  assert.strictEqual(reflectionPayload.user_message, requestText.substring(0, 500));
  assert.strictEqual(reflectionPayload.assistant_message, 'Gemini memory response');
}

function testAcpReflectionOwnership() {
  const source = fs.readFileSync('core/js/providers/copilot.js', 'utf8');
  assert.ok(source.includes('_copilotRenderResponse(data, txtOutput, modelLabel, question, signalContext, true, payload.session_id, turnId)'));
  assert.ok(source.includes('if (!reflectionHandledByBridge && content && userMessage)'));
  assert.ok(source.includes('session_id: reflectionSessionId ||'));
  for (const path of ['core/js/providers/aig.js', 'core/js/providers/openai.js', 'core/js/providers/gemini.js', 'core/js/providers/lm-studio.js', 'core/js/providers/copilot.js']) {
    assert.ok(fs.readFileSync(path, 'utf8').includes('turn_id: turnId'), path + ' must reuse the captured turn ID for reflection');
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
