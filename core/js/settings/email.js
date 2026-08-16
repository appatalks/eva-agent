// Email Settings workflow: mailbox records, session credentials, approved
// recipients, and sending. Credentials are handed to the bridge and never
// stored in localStorage or written back into the DOM.
var EvaEmailSettings = (function() {
  var state = { accounts: [], allowlist: [] };

  function el(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(value == null ? '' : String(value)));
    return div.innerHTML;
  }

  function status(id, message, isError) {
    var target = el(id);
    if (!target) return;
    target.textContent = message || '';
    target.style.color = isError ? '#d66' : '';
  }

  function bridge(path, options) {
    if (typeof backgroundBridgeRequest !== 'function') {
      return Promise.reject(new Error('Bridge unavailable'));
    }
    return backgroundBridgeRequest(path, options);
  }

  function splitList(value) {
    return String(value || '')
      .split(/[,\s]+/)
      .map(function(entry) { return entry.trim().toLowerCase(); })
      .filter(Boolean);
  }

  function render() {
    var list = el('emailAccountList');
    if (!list) return;
    if (!state.accounts.length) {
      list.innerHTML = '<p class="auth-note">No mailbox configured yet.</p>';
      return;
    }
    var html = '';
    state.accounts.forEach(function(account) {
      var abilities = (account.capabilities || []).join(' / ') || 'none';
      var signedIn = account.credential_present;
      var stateText = account.status === 'connected' && !signedIn
        ? 'needs sign-in'
        : account.status;
      html += '<div class="background-item" style="margin-bottom:8px;padding:8px;border:1px solid rgba(127,127,127,0.2);border-radius:6px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">';
      html += '<strong>' + escapeHtml(account.label) + '</strong>';
      html += '<span style="font-size:11px;opacity:0.7">' + escapeHtml(stateText) + '</span>';
      html += '</div>';
      html += '<div style="font-size:12px;opacity:0.8">' + escapeHtml(account.address) + '</div>';
      html += '<div style="font-size:11px;opacity:0.6">id: ' + escapeHtml(account.id)
        + ' · ' + escapeHtml(account.backend) + ' · ' + escapeHtml(abilities);
      if (account.morning_pull) html += ' · morning briefing';
      html += '</div></div>';
    });
    list.innerHTML = html;
  }

  async function refresh() {
    try {
      var data = await bridge('/v1/email/accounts', { method: 'GET' });
      state.accounts = (data && data.accounts) || [];
      state.allowlist = (data && data.allowlist) || [];
      var allowInput = el('emailAllowlist');
      if (allowInput && !allowInput.dataset.dirty) allowInput.value = state.allowlist.join(', ');
      render();
      status('emailAccountStatus', '');
    } catch (error) {
      status('emailAccountStatus', 'Could not read mailboxes: ' + (error && error.message), true);
    }
  }

  function collectAccount() {
    var backend = (el('emailAccountBackend') || {}).value || '';
    var account = {
      id: (el('emailAccountId') || {}).value || '',
      label: (el('emailAccountLabel') || {}).value || '',
      address: (el('emailAccountAddress') || {}).value || '',
      status: 'connected'
    };
    if (backend) account.backend = backend;

    if (backend === 'eva_direct') {
      var domains = splitList((el('emailInternalDomains') || {}).value);
      account.settings = {
        delivery_mode: 'internal',
        internal_domains: domains,
        internal_smtp_host: (el('emailInternalHost') || {}).value || '127.0.0.1',
        internal_smtp_port: Number((el('emailInternalPort') || {}).value) || 25,
        internal_smtp_starttls: !!(el('emailInternalStarttls') || {}).checked,
        direct_consent: splitList((el('emailDirectConsent') || {}).value)
      };
    } else {
      var settings = {};
      var imapHost = (el('emailImapHost') || {}).value;
      var smtpHost = (el('emailSmtpHost') || {}).value;
      var smtpPort = Number((el('emailSmtpPort') || {}).value);
      if (imapHost) settings.imap_host = imapHost;
      if (smtpHost) settings.smtp_host = smtpHost;
      if (smtpPort) settings.smtp_port = smtpPort;
      settings.smtp_starttls = !!(el('emailSmtpStarttls') || {}).checked;
      account.settings = settings;
      account.morning_pull = !!(el('emailMorningPull') || {}).checked;
    }
    return account;
  }

  async function saveAccounts(accounts, allowlist) {
    var payload = { accounts: accounts };
    if (allowlist) payload.allowlist = allowlist;
    var data = await bridge('/v1/email/accounts', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    if (data && data.errors && data.errors.length) {
      throw new Error(data.errors.join('; '));
    }
    return data;
  }

  async function saveAccount() {
    var account = collectAccount();
    if (!account.id || !account.address) {
      status('emailAccountStatus', 'An account id and address are required.', true);
      return;
    }
    var others = state.accounts.filter(function(entry) { return entry.id !== account.id; });
    try {
      await saveAccounts(others.concat([account]));
      status('emailAccountStatus', 'Saved ' + account.id + '.');
      await refresh();
    } catch (error) {
      status('emailAccountStatus', 'Rejected: ' + (error && error.message), true);
    }
  }

  async function removeAccount() {
    var id = (el('emailAccountId') || {}).value;
    if (!id) {
      status('emailAccountStatus', 'Enter the account id to remove.', true);
      return;
    }
    try {
      await saveAccounts(state.accounts.filter(function(entry) { return entry.id !== id; }));
      status('emailAccountStatus', 'Removed ' + id + '.');
      await refresh();
    } catch (error) {
      status('emailAccountStatus', 'Could not remove: ' + (error && error.message), true);
    }
  }

  async function saveCredential() {
    var field = el('emailCredential');
    var id = (el('emailCredentialId') || {}).value;
    var secret = field ? field.value : '';
    if (!id || !secret) {
      status('emailCredentialStatus', 'An account id and credential are required.', true);
      return;
    }
    try {
      await bridge('/v1/email/credential', {
        method: 'POST',
        body: JSON.stringify({ account_id: id, credential: secret })
      });
      if (field) field.value = '';
      status('emailCredentialStatus', 'Stored for this session.');
      await refresh();
    } catch (error) {
      status('emailCredentialStatus', 'Rejected: ' + (error && error.message), true);
    }
  }

  async function saveAllowlist() {
    var input = el('emailAllowlist');
    try {
      await saveAccounts(state.accounts, splitList(input ? input.value : ''));
      if (input) delete input.dataset.dirty;
      status('emailAccountStatus', 'Approved recipients updated.');
      await refresh();
    } catch (error) {
      status('emailAccountStatus', 'Rejected: ' + (error && error.message), true);
    }
  }

  async function send(request) {
    var message = {
      to: request.to,
      subject: request.subject,
      body: request.body
    };
    var payload = { message: message };
    if (request.account_id) payload.account_id = request.account_id;
    var result = await bridge('/v1/email/send', {
      method: 'POST',
      body: JSON.stringify(payload)
    });

    if (result && result.decision === 'needs_confirmation') {
      var unknown = (result.unknown_recipients || []).join(', ');
      var approved = typeof confirm === 'function'
        ? confirm('Send this message to ' + unknown + '?\n\nThey are not on the approved list.')
        : false;
      if (!approved) return { decision: 'cancelled' };
      payload.confirmation = { digest: result.digest, addresses: result.unknown_recipients };
      result = await bridge('/v1/email/send', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
    }
    return result;
  }

  async function sendFromForm() {
    var request = {
      account_id: (el('emailSendFrom') || {}).value || '',
      to: (el('emailSendTo') || {}).value || '',
      subject: (el('emailSendSubject') || {}).value || '',
      body: (el('emailSendBody') || {}).value || ''
    };
    status('emailSendStatus', 'Sending…');
    try {
      var result = await send(request);
      var decision = result && result.decision;
      if (decision === 'sent') {
        status('emailSendStatus', 'Sent.');
        if (el('emailSendBody')) el('emailSendBody').value = '';
      } else if (decision === 'partially_sent') {
        status('emailSendStatus', 'Partly sent. Not delivered: ' + (result.failures || []).join('; '), true);
      } else if (decision === 'cancelled') {
        status('emailSendStatus', 'Cancelled.');
      } else {
        status('emailSendStatus', 'Not sent: ' + ((result && result.reason) || 'refused'), true);
      }
    } catch (error) {
      status('emailSendStatus', 'Failed: ' + (error && error.message), true);
    }
  }

  function toggleBackendFields() {
    var backend = (el('emailAccountBackend') || {}).value;
    var direct = backend === 'eva_direct';
    if (el('emailDirectFields')) el('emailDirectFields').hidden = !direct;
    if (el('emailImapFields')) el('emailImapFields').hidden = direct;
  }

  function bind() {
    var handlers = [
      ['emailRefresh', refresh],
      ['emailSaveAccount', saveAccount],
      ['emailRemoveAccount', removeAccount],
      ['emailSaveCredential', saveCredential],
      ['emailSaveAllowlist', saveAllowlist],
      ['emailSendMessage', sendFromForm]
    ];
    handlers.forEach(function(entry) {
      var button = el(entry[0]);
      if (button && !button.dataset.bound) {
        button.addEventListener('click', entry[1]);
        button.dataset.bound = '1';
      }
    });
    var backend = el('emailAccountBackend');
    if (backend && !backend.dataset.bound) {
      backend.addEventListener('change', toggleBackendFields);
      backend.dataset.bound = '1';
    }
    var allow = el('emailAllowlist');
    if (allow && !allow.dataset.bound) {
      allow.addEventListener('input', function() { allow.dataset.dirty = '1'; });
      allow.dataset.bound = '1';
    }
  }

  function open() {
    if (typeof openSettingsTab === 'function') openSettingsTab('email');
    bind();
    toggleBackendFields();
    return refresh();
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', function() {
      bind();
      toggleBackendFields();
    });
  }

  return {
    open: open,
    refresh: refresh,
    send: send,
    accounts: function() { return state.accounts.slice(); }
  };
}());
