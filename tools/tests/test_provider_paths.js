#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');

const html = fs.readFileSync('index.html', 'utf8');
const providers = [
  ['core/js/providers/openai.js', 'trboSend'],
  ['core/js/providers/gemini.js', 'geminiSend'],
  ['core/js/providers/lm-studio.js', 'lmsSend'],
  ['core/js/providers/copilot.js', 'copilotSend'],
  ['core/js/providers/aig.js', 'aigSend'],
  ['core/js/providers/image-generation.js', 'dalle3Send']
];

const optionsIndex = html.indexOf('src="core/js/options.js');
assert.ok(optionsIndex >= 0, 'options.js must remain loaded');
providers.forEach(([path, sender]) => {
  const scriptIndex = html.indexOf(`src="${path}`);
  assert.ok(scriptIndex > optionsIndex, `${path} must load after shared options globals`);
  const source = fs.readFileSync(path, 'utf8');
  assert.match(source, new RegExp(`(?:async\\s+)?function\\s+${sender}\\s*\\(`), `${sender} must remain declared in ${path}`);
});

console.log('provider path tests: PASS');