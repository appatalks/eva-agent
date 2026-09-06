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
const optionsSource = fs.readFileSync(path.join(root, 'core/js/options.js'), 'utf8');
const aigSource = fs.readFileSync(path.join(root, 'core/js/providers/aig.js'), 'utf8');
const cognitionSource = fs.readFileSync(path.join(root, 'core/js/cognition.js'), 'utf8');
const aigContext = {};
vm.createContext(aigContext);
vm.runInContext(aigSource, aigContext);

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
check('hosted-provider experimentation is visibly disabled',
  /Gmail, Outlook\/Microsoft OAuth, Work IQ, and remote-MCP mailbox connections are experimental and disabled/.test(html));
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
check('exposes pending email operations', api
  && typeof api.prepare === 'function'
  && typeof api.confirmPending === 'function'
  && typeof api.cancelPending === 'function'
  && typeof api.hasPending === 'function');
check('exposes accounts()', api && typeof api.accounts === 'function');
check('accounts() starts empty', Array.isArray(api.accounts()) && api.accounts().length === 0);

console.log('\ncredential handling');
const codeOnly = moduleSource.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
check('credential is never written to browser storage',
  !/(?:localStorage|sessionStorage)\.setItem\([^)]*credential/i.test(codeOnly));
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
  /pending_confirmation/.test(sendFunction) && /evaConfirmAction/.test(sendFunction));
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
check('confirmation echoes only the opaque pending id',
  /pending_id: result\.pending_id/.test(sendFunction) && !/digest: result\.digest/.test(moduleSource));
check('pending browser storage contains only opaque identifiers',
  /sessionStorage\.setItem\(pendingStorageKey\(sessionId\), pendingId\)/.test(moduleSource)
    && /localStorage\.setItem\(pendingStorageKey\(sessionId\), pendingId\)/.test(moduleSource)
    && !/sessionStorage\.setItem[^\n]*(?:subject|body|recipient|message)/i.test(moduleSource));
check('terse chat confirmation bypasses model routing only with pending state',
  /function pendingEmailCommand\(text\)/.test(aigSource)
    && /EvaEmailSettings\.hasPending\(sessionId\)/.test(aigSource)
    && /EvaEmailSettings\.confirmPending\(sessionId\)/.test(aigSource));
check('reported confirmation phrase resolves without capturing unrelated approval',
  aigContext.pendingEmailCommand('confirmed. Please send.') === 'confirm'
    && aigContext.pendingEmailCommand('Confirmed, please continue') === 'confirm'
    && aigContext.pendingEmailCommand('yes, continue the coding task') === ''
    && aigContext.explicitPendingEmailCommand('confirmed. Please send.', 'confirm')
    && aigContext.explicitPendingEmailCommand('Confirmed, please continue', 'confirm')
    && aigContext.pendingEmailCommand('Confrimed') === 'confirm'
    && aigContext.explicitPendingEmailCommand('Confrimed', 'confirm')
    && !aigContext.explicitPendingEmailCommand('yes', 'confirm'));
check('chat reports failed local transport as undelivered',
  /recipient did not receive it/.test(aigContext.pendingEmailResultContent({
    decision: 'submitted', transport_status: { status: 'failed', detail: 'route unavailable' }
  }, 'confirm')));
check('explicit confirmation can recover from bridge state without a browser token',
  !/if \(!pendingId\) return \{ decision: 'none' \}/.test(moduleSource));
const reportedTestDraft = aigContext.requestedTestEmailDraft(
  'Hi Eva, please try sending a test email to peer@example.com'
);
check('reported test-email request prepares a deterministic draft',
  reportedTestDraft
    && reportedTestDraft.to === 'peer@example.com'
    && reportedTestDraft.subject === 'Test email'
    && reportedTestDraft.body === 'This is a test email from Eva.');
check('deterministic test-email preparation rejects negation and ambiguity',
  !aigContext.requestedTestEmailDraft("Don't send a test email to peer@example.com")
    && !aigContext.requestedTestEmailDraft('Send a test email to a@example.com and b@example.com'));
const contextualDraft = aigContext.contextualTestEmailDraft(
  'I just want you to send a test, nothing to big',
  [{ role: 'user', content: 'Please send a test email to peer@example.com' }]
);
check('terse test continuation uses one recent user-supplied email address',
  contextualDraft && contextualDraft.to === 'peer@example.com');
const localhostRevision = aigContext.contextualTestEmailDraft(
  "Cool let's deliver to localhost@localhost",
  [{ role: 'user', content: 'Please send a test email to peer@example.com' }]
);
check('test-email recipient can be revised to an explicit localhost address',
  localhostRevision && localhostRevision.to === 'localhost@localhost');
check('contextual test continuation rejects assistant-only or ambiguous addresses',
  !aigContext.contextualTestEmailDraft('Send a test', [{ role: 'assistant', content: 'peer@example.com' }])
    && !aigContext.contextualTestEmailDraft('Send a test', [
      { role: 'user', content: 'Email a@example.com' },
      { role: 'user', content: 'Email b@example.com' }
    ]));
check('Cognition can prepare but not directly send email',
  /id: 'email\.prepare'/.test(cognitionSource)
    && /action: 'prepare_email'/.test(cognitionSource)
    && !/id: 'email\.send'/.test(cognitionSource));
check('pending email blocks browser and desktop automation',
  /var nativeEmailPending =/.test(optionsSource)
    && /var nativeVisualForbidden = nativeEmailPending \|\|/.test(optionsSource));
check('email goals are blocked inside browser and desktop markers',
  /function isEmailAutomationGoal\(value\)/.test(optionsSource)
    && /native mail capability instead of browser automation/.test(optionsSource)
    && /native mail capability instead of desktop automation/.test(optionsSource));
check('declining does not send', /decision: 'cancelled'/.test(moduleSource));
check('partial delivery is surfaced', /partially_sent/.test(moduleSource));
check('local submission is not mislabeled as delivery',
  /decision === 'submitted'/.test(moduleSource) && /Final (?:inbox )?delivery is not verified/.test(moduleSource));
check('local submission reports automatic Exim transport status',
  /transport_status/.test(moduleSource)
    && /Exim could not deliver/.test(moduleSource)
    && /next SMTP hop/.test(moduleSource));
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
check('prepare_email action declared', /id: 'prepare_email'/.test(harnessSource));
check('send_email is not advertised to models', !/id: 'send_email'/.test(harnessSource));
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
