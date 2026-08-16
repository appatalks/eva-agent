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
const dialogsSource = fs.readFileSync(path.join(root, 'core/js/dialogs.js'), 'utf8');
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
check('sender is chosen from a list, not typed',
  /<select id="emailSendFrom"/.test(html));
check('credential account is chosen from a list, not typed',
  /<select id="emailCredentialId"/.test(html));
check('allowlist field present', /id="emailAllowlist"/.test(html));
check('account list region is live', /id="emailAccountList"[^>]*aria-live/.test(html));
check('account list has a new-mailbox action', /id="emailNewAccount"/.test(html));
check('best-effort local MTA mode is available',
  /id="emailDirectMode"/.test(html) && /value="local_mta"/.test(html));
check('Exim inspection requires explicit opt-in controls',
  /id="emailEximStatus"/.test(html) && /id="emailEximStatusSudo"/.test(html));
check('local MTA status check control exists', /id="emailCheckMtaStatus"/.test(html));

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
const sendFunction = moduleSource.slice(
  moduleSource.indexOf('async function send(request)'),
  moduleSource.indexOf('async function sendFromForm()')
);
check('unapproved recipients trigger the native confirmation surface',
  /needs_confirmation/.test(sendFunction) && /evaConfirmAction/.test(sendFunction));
check('email confirmation does not use generic browser confirm',
  !/\bconfirm\(/.test(sendFunction));
check('confirmation displays normalized To, Cc, Bcc, subject, and body preview',
  /normalized\.to/.test(sendFunction)
    && /normalized\.cc/.test(sendFunction)
    && /normalized\.bcc/.test(sendFunction)
    && /normalized\.subject/.test(sendFunction)
    && /bodyPreview/.test(sendFunction));
check('confirmation warns that final delivery is not verified',
  /final delivery is not verified/i.test(sendFunction));
check('confirmation echoes the server digest',
  /digest: result\.digest/.test(moduleSource));
check('declining does not send', /decision: 'cancelled'/.test(moduleSource));
check('partial delivery is surfaced', /partially_sent/.test(moduleSource));
check('local submission is not mislabeled as delivery',
  /decision === 'submitted'/.test(moduleSource) && /Final delivery is not verified/.test(moduleSource));
check('submitted local MTA queue id enables status inspection',
  /lastMtaSubmission/.test(moduleSource) && /mta_queue_id/.test(moduleSource) && /checkMtaStatus/.test(moduleSource));
check('status lookup uses account and queue id only',
  /\/v1\/email\/exim-status\?account_id=/.test(moduleSource));
check('account pickers are populated from live accounts',
  /function fillAccountPickers\(\)/.test(moduleSource));
check('Eva direct is excluded from the credential picker',
  /capability === 'credential'\) return account\.backend !== 'eva_direct'/.test(moduleSource));
check('Eva direct is not shown as needing sign-in',
  /account\.backend !== 'eva_direct' && !signedIn/.test(moduleSource));
check('Eva identity setup requires local domains and consent',
  /at least one local domain/.test(moduleSource) && /accepts mail from Eva/.test(moduleSource));
check('existing mailbox rows expose an edit action',
  /data-email-edit/.test(moduleSource));
check('unsupported provider and relay accounts are not editable',
  /function editableAccount\(account\)/.test(moduleSource)
    && /account\.backend === 'imap_smtp'/.test(moduleSource)
    && /delivery_mode/.test(moduleSource)
    && /Managed by provider setup/.test(moduleSource));
check('editing loads direct consent and local domains',
  /function loadAccount\(id\)/.test(moduleSource)
    && /settings\.direct_consent/.test(moduleSource)
    && /settings\.internal_domains/.test(moduleSource));
check('direct consent also becomes the account-scoped allowlist',
  /account\.allowlist = directConsent/.test(moduleSource));
check('editing starts from the complete persisted account record',
  /JSON\.parse\(JSON\.stringify\(original\)\)/.test(moduleSource));
check('editing does not force existing accounts back to connected',
  /original \? JSON\.parse/.test(moduleSource)
    && !/account\.status = 'connected'/.test(moduleSource));
check('saving one mailbox uses the focused endpoint',
  /bridge\('\/v1\/email\/account'/.test(moduleSource));
check('saving global recipients uses the focused endpoint',
  /bridge\('\/v1\/email\/allowlist'/.test(moduleSource));
check('deleting one mailbox uses its focused endpoint',
  /\/v1\/email\/accounts\/.*encodeURIComponent/.test(moduleSource));
check('frontend does not use full-document account replacement',
  !/bridge\('\/v1\/email\/accounts',\s*\{\s*method: 'POST'/.test(moduleSource));

console.log('\npanel lifecycle');
const optionsSource = fs.readFileSync(path.join(root, 'core/js/options.js'), 'utf8');
check('opening the Email tab loads mailboxes',
  /target === 'email' && window\.EvaEmailSettings\) EvaEmailSettings\.refresh\(\)/.test(optionsSource));
check('open() opens settings through the real button',
  /'evaSettingsBtn'/.test(moduleSource));
check('open() activates the email tab',
  /\.settings-tab\[data-stab="email"\]/.test(moduleSource));
check('open() does not call a nonexistent helper',
  !/openSettingsTab/.test(moduleSource));

console.log('\nnative confirmation dialog');
check('confirmation dialog markup exists',
  /id="evaActionConfirm"/.test(html)
    && /id="evaActionConfirmDetails"/.test(html)
    && /id="evaActionConfirmWarning"/.test(html));
check('native confirmation uses textContent for message details',
  /details\.textContent/.test(dialogsSource) && /warning\.textContent/.test(dialogsSource));
check('native confirmation returns false when cancelled',
  /_closeEvaActionConfirm\(false\)/.test(dialogsSource));

console.log('\nnative harness');check('email navigation target registered',
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
