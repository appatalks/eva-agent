#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const calls = [];
const responses = [];
const context = {
  JSON,
  Promise,
  async detectACPBridge() { return 'http://localhost:8888/'; },
  fetch(url, options) {
    calls.push({ url, options });
    return Promise.resolve(responses.shift());
  }
};
context.window = context;
vm.runInNewContext(fs.readFileSync('core/js/runtime/bridge-client.js', 'utf8'), context, {
  filename: 'core/js/runtime/bridge-client.js'
});

async function main() {
  responses.push({ ok: true, text: async () => '{"value":1}' });
  const data = await context.backgroundBridgeRequest('/v1/test', { method: 'POST' });
  assert.deepStrictEqual(JSON.parse(JSON.stringify(data)), { value: 1 });
  assert.strictEqual(calls[0].url, 'http://localhost:8888/v1/test');
  assert.strictEqual(calls[0].options.method, 'POST');

  responses.push({ ok: false, status: 403, text: async () => '{"error":{"message":"Denied"}}' });
  await assert.rejects(
    context.backgroundBridgeRequest('/v1/private'),
    (error) => error.message === 'Denied' && error.status === 403 && error.data.error.message === 'Denied'
  );

  responses.push({ ok: false, status: 500, text: async () => 'Bridge failed' });
  await assert.rejects(
    context.backgroundBridgeRequest('/v1/broken'),
    (error) => error.message === 'Bridge failed' && error.status === 500 && error.data.message === 'Bridge failed'
  );
}

main().then(function() {
  console.log('bridge client tests: PASS');
}).catch(function(error) {
  console.error(error);
  process.exitCode = 1;
});