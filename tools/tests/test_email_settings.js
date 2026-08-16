#!/usr/bin/env node
'use strict';
// Contract: email settings surface and native harness controls.
// Verifies the markup, the module's public API, and that Eva can reach email
// natively without simulated input.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const moduleSource = fs.readFileSync(path.join(root, 'core/js/settings/email.js'), 'utf8');
const harnessSource = fs.readFileSync(path.join(root, 'core/js/harness-control.js'), 'utf8');

let failures = 0;
function check(name, condition) {
  if (condition) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name}`);
  }
}

console.log('markup');
check('email settings tab exists', /data-stab="email"[^>]*role="tab"/.test(html));
check('email settings panel exists', /class="settings-panel" data-stab="email"/.test(html));
check('module is loaded in index.html', /core\/js\/settings\/email\.js/.test(html));
check('module loads before harness control',
  html.indexOf('core/js/settings/email.js') < html.indexOf('core/js/harness-control.js'));
check('credential field is a password input',
  /<input type="password" id="emailCredential"/.test(html));
check('credential field disables autocomplete',
  /id="emailCredential"[^>]*autocomplete="off"/.test(html));
check('send form present', /id="emailSendTo"/.test(html) && /id="emailSendMessage"/.test(html));
check('allowlist field present', /id="emailAllowlist"/.test(html));
check('account list region is live', /id="emailAccountList"[^>]*aria-live/.test(html));

console.log('\nmodule');
const context = {
  console,
  document: {
    getElementById: () => null,
    createElement: () => ({ appendChild() {}, innerHTML: '' }),
    createTextNode: (t) => t,
    addEventListener: () => {},
  },
  window: {},
  backgroundBridgeRequest: async () => ({ accounts: [], allowlist: [] }),
};
context.window = context;
vm.createContext(context);
vm.runInContext(moduleSource, context);
const api = context.EvaEmailSettings;

check('exposes open()', api && typeof api.open === 'function');
check('exposes refresh()', api && typeof api.refresh === 'function');
check('exposes send()', api && typeof api.send === 'function');
check('exposes accounts()', api && typeof api.accounts === 'function');
check('accounts() starts empty', Array.isArray(api.accounts()) && api.accounts().length === 0);

console.log('\ncredential handling');
const codeOnly = moduleSource.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
check('credential is never written to browser storage',
  !/localStorage\s*[.[]/.test(codeOnly) && !/sessionStorage\s*[.[]/.test(codeOnly));
check('credential field is cleared after storing',
  /field\.value = ''/.test(moduleSource));
check('credential is sent only to the credential endpoint',
  /\/v1\/email\/credential/.test(moduleSource));

console.log('\nsend policy');
check('unapproved recipients trigger a confirmation prompt',
  /needs_confirmation/.test(moduleSource) && /confirm\(/.test(moduleSource));
check('confirmation echoes the server digest',
  /digest: result\.digest/.test(moduleSource));
check('declining does not send', /decision: 'cancelled'/.test(moduleSource));
check('partial delivery is surfaced', /partially_sent/.test(moduleSource));

console.log('\nnative harness');
check('email navigation target registered',
  /email: function\(\) \{ return openSettings\('email'\); \}/.test(harnessSource));
check('mail aliases resolve to email',
  /mail: 'email'/.test(harnessSource) && /inbox: 'email'/.test(harnessSource));
check('describe_email action declared', /id: 'describe_email'/.test(harnessSource));
check('send_email action declared', /id: 'send_email'/.test(harnessSource));
check('send_email requires an explicit request',
  /explicit direct user request[^']*args: \{to, subject, body/.test(harnessSource));
check('send_email validates required fields',
  /A recipient, subject, and message are required/.test(harnessSource));
check('send_email reports a declined confirmation',
  /You declined the unapproved recipient/.test(harnessSource));
check('harness remains native-only', /nativeOnly: true/.test(harnessSource));

console.log('');
if (failures) {
  console.log(`email settings tests: FAIL (${failures})`);
  process.exit(1);
}
console.log('email settings tests: PASS');
