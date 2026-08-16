#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const EventEmitter = require('events');

const { RuntimeLogger, redactRuntimeText } = require('../../standalone/runtime-logger');

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-log-'));
const privateKeyHeader = '-----BEGIN ' + 'PRIVATE KEY-----';
const privateKeyFooter = '-----END ' + 'PRIVATE KEY-----';
const privateKeyFixture = 'PRIVATE_KEY=' + privateKeyHeader + '\nMII-private-key-material\n' + privateKeyFooter;
const githubPatFixture = 'github_pat_' + 'abcdefghijklmnopqrstuvwxyz';

function read(file) {
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
}

try {
  const logPath = path.join(temporary, 'eva-runtime.log');
  const logger = new RuntimeLogger({ logPath, maxBytes: 64 * 1024, backups: 2, lineLimit: 512 });

  logger.write('test', 'info', 'hello runtime');
  logger.write('test', 'error', 'Authorization: Bearer super-secret-token');
  logger.write('test', 'error', 'Authorization: Basic opaque-basic-value');
  logger.write('test', 'error', 'Authorization: Token opaque-token-value');
  logger.write('test', 'info', 'password=hunter2 token=abc123');
  logger.write('test', 'info', 'https://example.com/?token=secret-query');
  logger.write('test', 'info', 'PREFIX_TOKEN=prefixed-secret REFRESH_TOKEN=refresh-secret');
  logger.write('test', 'info', 'PRIVATE_KEY=private-key-material');
  logger.write('test', 'info', privateKeyFixture);
  logger.write('test', 'info', 'https://example.com/callback?access_token=oauth-access&refresh_token=oauth-refresh');
  logger.write('test', 'info', 'https://example.com/callback?code=oauth-authorization-code&device_code=device-secret');
  logger.write('test', 'info', 'Kusto cluster: https://private-cluster.kusto.windows.net/private-path');
  logger.write('test', 'info', '"body":"' + 'x'.repeat(160) + '"');
  logger.write('test', 'info', JSON.stringify({
    model: 'example',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'private multimodal prompt' }] }],
    client_secret: 'json-secret'
  }));
  logger.write('test', 'info', '[Audit] {"assistant_response":"ordinary private answer text","event":"turn.response"}');
  logger.write('test', 'info', JSON.stringify({ content: 'private short prompt' }));
  logger.write('test', 'info', 'request failed: ' + JSON.stringify({
    messages: [{ content: [{ type: 'input_text', text: 'decorated private prompt' }] }]
  }) + ' status=400');
  logger.write('test', 'info', JSON.stringify({
    system_prompt: 'private system prompt',
    developer_prompt: 'private developer prompt',
    user_content: 'private user content'
  }));
  logger.write('test', 'info', 'prefix {\\"content\\":\\"escaped private content\\"} suffix');
  logger.write('test', 'info', 'prefix {\\"system_prompt\\":\\"escaped private prompt\\"} suffix');
  logger.write('test', 'info', 'prefix {\\"content\\":\\"first \\\\\\"private-tail\\"} suffix');
  logger.write('test', 'info', '[eva-acp] [Cognition/SQLite] Auto-summary: private plain summary');
  logger.write('test', 'info', '[eva-acp] [MCP:example] private server diagnostic');

  const renderer = new EventEmitter();
  logger.attachRenderer(renderer);
  renderer.emit('console-message', {}, { level: 3, message: 'renderer failed', sourceId: 'file:///app/email.js', lineNumber: 42 });
  renderer.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 9 });

  let contents = read(logPath);
  assert(contents.includes('[test] hello runtime'));
  assert(contents.includes('[renderer] renderer failed email.js:42'));
  assert(contents.includes('process_gone'));
  assert(!contents.includes('super-secret-token'));
  assert(!contents.includes('opaque-basic-value'));
  assert(!contents.includes('opaque-token-value'));
  assert(!contents.includes('hunter2'));
  assert(!contents.includes('secret-query'));
  assert(!contents.includes('prefixed-secret'));
  assert(!contents.includes('refresh-secret'));
  assert(!contents.includes('private-key-material'));
  assert(!contents.includes('MII-private-key-material'));
  assert(!contents.includes('BEGIN PRIVATE KEY'));
  assert(!contents.includes('oauth-access'));
  assert(!contents.includes('oauth-refresh'));
  assert(!contents.includes('oauth-authorization-code'));
  assert(!contents.includes('device-secret'));
  assert(!contents.includes('private-cluster'));
  assert(!contents.includes('private multimodal prompt'));
  assert(!contents.includes('json-secret'));
  assert(!contents.includes('private short prompt'));
  assert(!contents.includes('decorated private prompt'));
  assert(!contents.includes('private system prompt'));
  assert(!contents.includes('private developer prompt'));
  assert(!contents.includes('private user content'));
  assert(!contents.includes('ordinary private answer text'));
  assert(!contents.includes('escaped private content'));
  assert(!contents.includes('escaped private prompt'));
  assert(!contents.includes('private-tail'));
  assert(!contents.includes('private plain summary'));
  assert(!contents.includes('private server diagnostic'));
  assert(!contents.includes('x'.repeat(100)));
  assert(contents.includes('<redacted>'));
  assert.strictEqual(fs.statSync(logPath).mode & 0o777, 0o600);

  const originalStdout = process.stdout.write;
  const originalStderr = process.stderr.write;
  logger.installProcessStreams();
  process.stdout.write('captured stdout\n');
  process.stderr.write('captured stderr\n');
  assert.notStrictEqual(process.stdout.write, originalStdout);
  logger.close();
  assert.strictEqual(process.stdout.write, originalStdout);
  assert.strictEqual(process.stderr.write, originalStderr);
  contents = read(logPath);
  assert(contents.includes('captured stdout'));
  assert(contents.includes('captured stderr'));
  assert(contents.includes('session_end'));

  const rotatePath = path.join(temporary, 'rotate.log');
  const rotating = new RuntimeLogger({ logPath: rotatePath, maxBytes: 1024, backups: 2, lineLimit: 400 });
  for (let index = 0; index < 20; index += 1) {
    rotating.write('rotation', 'info', 'line-' + index + '-' + 'z'.repeat(180));
  }
  rotating.close();
  assert(fs.existsSync(rotatePath));
  assert(fs.existsSync(rotatePath + '.1'));
  assert(fs.statSync(rotatePath).size <= 1400);
  assert(fs.statSync(rotatePath + '.1').size <= 1400);

  assert.strictEqual(redactRuntimeText('Bearer abcdef', 500), 'Bearer <redacted>');
  assert.strictEqual(redactRuntimeText('Authorization: Basic abcdef', 500), 'Authorization: <redacted>');
  assert.strictEqual(redactRuntimeText('Authorization: Token abcdef', 500), 'Authorization: <redacted>');
  assert(!redactRuntimeText(githubPatFixture, 500).includes('github_pat_'));
  assert(!redactRuntimeText('"credential":"private-value"', 500).includes('private-value'));
  assert(!redactRuntimeText('AZURE_ACCESS_TOKEN=private-value', 500).includes('private-value'));
  assert(!redactRuntimeText('PRIVATE_KEY=private-value', 500).includes('private-value'));
  assert(!redactRuntimeText(
    'PRIVATE_KEY=' + privateKeyHeader + '\nMII-private-value\n' + privateKeyFooter, 500
  ).includes('MII-private-value'));
  assert(!redactRuntimeText('https://x.test/?refresh_token=private-value', 500).includes('private-value'));
  assert(!redactRuntimeText('https://x.test/callback?code=private-value', 500).includes('private-value'));
  assert.strictEqual(
    redactRuntimeText('https://private-cluster.kusto.windows.net/', 500),
    '<redacted-kusto-endpoint>'
  );
  assert(!redactRuntimeText(JSON.stringify({ messages: [{ content: [{ text: 'private-value' }] }] }), 500).includes('private-value'));
  assert(!redactRuntimeText('{"content":"private-value"}', 500).includes('private-value'));
  assert(!redactRuntimeText('prefix {"messages":[{"content":[{"text":"private-value"}]}]} suffix', 500).includes('private-value'));
  assert(!redactRuntimeText('{"system_prompt":"private-value"}', 500).includes('private-value'));
  assert(!redactRuntimeText('{"developer_prompt":"private-value"}', 500).includes('private-value'));
  assert(!redactRuntimeText('{"user_content":"private-value"}', 500).includes('private-value'));
  assert(!redactRuntimeText('prefix {\\"content\\":\\"private-value\\"} suffix', 500).includes('private-value'));
  assert(!redactRuntimeText('prefix {\\"content\\":\\"first \\\\\\"private-tail\\"} suffix', 500).includes('private-tail'));

  console.log('runtime logger tests: PASS');
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}
