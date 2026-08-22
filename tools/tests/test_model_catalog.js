#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('index.html', 'utf8');
const options = fs.readFileSync('core/js/options.js', 'utf8');
const aig = fs.readFileSync('core/js/providers/aig.js', 'utf8');
const cognition = fs.readFileSync('core/js/cognition.js', 'utf8');
const copilot = fs.readFileSync('core/js/providers/copilot.js', 'utf8');
const camera = fs.readFileSync('core/js/features/automation/camera.js', 'utf8');
const bridge = fs.readFileSync('tools/bridge/core.py', 'utf8');
const routingSource = fs.readFileSync('core/js/model-routing.js', 'utf8');
const settings = fs.readFileSync('core/js/settings/model-settings.js', 'utf8');

const select = html.match(/<select id="selModel"[^>]*>([\s\S]*?)<\/select>/);
assert.ok(select, 'model selector must exist');
const values = [...select[1].matchAll(/value="([^"]+)"/g)].map((match) => match[1]);
assert.deepStrictEqual(values, ['aig'], 'AIG must be the only top-level model route');

const routingSandbox = { window: {} };
vm.runInNewContext(routingSource, routingSandbox, { filename: 'core/js/model-routing.js' });
const routing = routingSandbox.window.EvaModelRouting;
assert.ok(routing, 'model routing utility must expose EvaModelRouting');
assert.strictEqual(routing.routeFor('aig'), 'aig');
for (const model of ['gpt-4o', 'copilot-acp', 'gemini', 'lm-studio', 'dall-e-3', 'unknown-model']) {
  assert.strictEqual(routing.routeFor(model), '', `${model} must not be a top-level route`);
}

const sendStart = options.indexOf('async function sendData()');
const sendEnd = options.indexOf('\nfunction evaAuditEvent', sendStart);
assert.notStrictEqual(sendStart, -1, 'sendData must exist');
assert.notStrictEqual(sendEnd, -1, 'sendData must end before evaAuditEvent');
const sendData = options.slice(sendStart, sendEnd);
assert.match(sendData, /await aigSend\(\)/, 'normal sends must use AIG');
assert.doesNotMatch(sendData, /(?:copilotSend|trboSend|geminiSend|lmsSend|dalle3Send)\(\)/,
  'normal sends must not dispatch directly to legacy providers');

assert.match(settings, /var allowed = new Set\(\['aig'\]\)/,
  'theme filtering must retain only AIG');
assert.doesNotMatch(copilot, /models\.github\.ai|GitHub Models API|_copilotSendModelsAPI/,
  'Copilot provider must not contain the deprecated model API');
assert.doesNotMatch(camera, /models\.github\.ai|GitHub Models API|_describeViaGitHubModels/,
  'camera provider must not contain the deprecated model API');
assert.doesNotMatch(bridge, /models\.github\.ai|_github_model_map|github-models/,
  'bridge must not contain the deprecated model responder');
assert.match(aig, /var storedMessages = newMessages\.map/,
  'image attachments must be kept out of persistent AIG history');
assert.match(aig, /image_b64: imageB64/,
  'AIG must forward the current image to the bridge');
assert.match(cognition, /image_b64: String\(\(extra && extra\.image_b64\)/,
  'cognition must forward images to its AIG stages');
assert.match(bridge, /prompt_with_image\(/,
  'ACP must receive multimodal AIG requests through its image capability');

console.log(`model catalog tests: PASS (${values.length} top-level route)`);
