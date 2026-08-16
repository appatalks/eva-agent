// Email Settings workflow: mailbox records, session credentials, approved
// recipients, and sending. Credentials are handed to the bridge and never
// stored in localStorage or written back into the DOM.
var EvaEmailSettings = (function() {
  var state = { accounts: [], allowlist: [], selectedId: '' };

  function el(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(value == null ? '' : String(value)));
    return div.innerHTML;
  }

  function editableAccount(account) {
    if (!account) return false;
    if (account.backend === 'imap_smtp') return true;
    return account.backend === 'eva_direct'
      && ((account.settings || {}).delivery_mode || 'internal') === 'internal';
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

  function fillAccountPickers() {
    [['emailSendFrom', 'send'], ['emailCredentialId', 'credential']].forEach(function(entry) {
      var select = el(entry[0]);
      if (!select) return;
      var capability = entry[1];
      var choices = state.accounts.filter(function(account) {
        if (capability === 'credential') return account.backend !== 'eva_direct';
        return !capability || (account.capabilities || []).indexOf(capability) >= 0;
      });
      var previous = select.value;
      select.innerHTML = choices.length
        ? choices.map(function(account) {
            return '<option value="' + escapeHtml(account.id) + '">'
              + escapeHtml(account.label) + ' — ' + escapeHtml(account.address) + '</option>';
          }).join('')
        : '<option value="">No eligible mailbox</option>';
      if (previous && choices.some(function(a) { return a.id === previous; })) select.value = previous;
    });
  }

  function render() {
    var list = el('emailAccountList');
    if (!list) return;
    fillAccountPickers();
    if (!state.accounts.length) {
      list.innerHTML = '<p class="auth-note">No mailbox configured yet.</p>';
      return;
    }
    var html = '';
    state.accounts.forEach(function(account) {
      var abilities = (account.capabilities || []).join(' / ') || 'none';
      var signedIn = account.credential_present;
      var stateText = account.status === 'connected' && account.backend !== 'eva_direct' && !signedIn
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
      html += '</div>';
      if (editableAccount(account)) {
        html += '<button type="button" class="auth-toggle" data-email-edit="'
          + escapeHtml(account.id) + '" style="margin-top:6px">Edit</button>';
      } else {
        html += '<span style="display:block;margin-top:6px;font-size:11px;opacity:0.6">Managed by provider setup</span>';
      }
      html += '</div>';
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
    } catch (error) {
      status('emailAccountStatus', 'Could not read mailboxes: ' + (error && error.message), true);
    }
  }

  function collectAccount() {
    var backend = (el('emailAccountBackend') || {}).value || '';
    var original = state.accounts.find(function(entry) { return entry.id === state.selectedId; });
    var account = original ? JSON.parse(JSON.stringify(original)) : { status: 'connected' };
    delete account.credential_present;
    account.id = (el('emailAccountId') || {}).value || '';
    account.label = (el('emailAccountLabel') || {}).value || '';
    account.address = (el('emailAccountAddress') || {}).value || '';
    if (backend) account.backend = backend;

    if (backend === 'eva_direct') {
      var domains = splitList((el('emailInternalDomains') || {}).value);
      var directConsent = splitList((el('emailDirectConsent') || {}).value);
      account.settings = Object.assign({}, account.settings || {}, {
        delivery_mode: (account.settings || {}).delivery_mode || 'internal',
        internal_domains: domains,
        internal_smtp_host: (el('emailInternalHost') || {}).value || '127.0.0.1',
        internal_smtp_port: Number((el('emailInternalPort') || {}).value) || 25,
        internal_smtp_starttls: !!(el('emailInternalStarttls') || {}).checked,
        direct_consent: directConsent
      });
      // Direct consent is stricter than ordinary recipient approval, so it also
      // serves as this account's allowlist without affecting other mailboxes.
      account.allowlist = directConsent;
    } else {
      var settings = Object.assign({}, account.settings || {});
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

  function loadAccount(id) {
    var account = state.accounts.find(function(entry) { return entry.id === id; });
    if (!editableAccount(account)) {
      status('emailAccountStatus', 'This mailbox is managed by its provider setup.', true);
      return;
    }
    state.selectedId = account.id;
    if (el('emailAccountId')) {
      el('emailAccountId').value = account.id;
      el('emailAccountId').disabled = true;
    }
    if (el('emailAccountLabel')) el('emailAccountLabel').value = account.label || '';
    if (el('emailAccountAddress')) el('emailAccountAddress').value = account.address || '';
    if (el('emailAccountBackend')) el('emailAccountBackend').value = account.backend || '';
    var settings = account.settings || {};
    if (el('emailImapHost')) el('emailImapHost').value = settings.imap_host || '';
    if (el('emailSmtpHost')) el('emailSmtpHost').value = settings.smtp_host || '';
    if (el('emailSmtpPort')) el('emailSmtpPort').value = settings.smtp_port || '';
    if (el('emailSmtpStarttls')) el('emailSmtpStarttls').checked = settings.smtp_starttls !== false;
    if (el('emailMorningPull')) el('emailMorningPull').checked = !!account.morning_pull;
    if (el('emailInternalHost')) el('emailInternalHost').value = settings.internal_smtp_host || '';
    if (el('emailInternalPort')) el('emailInternalPort').value = settings.internal_smtp_port || '';
    if (el('emailInternalStarttls')) el('emailInternalStarttls').checked = !!settings.internal_smtp_starttls;
    if (el('emailInternalDomains')) el('emailInternalDomains').value = (settings.internal_domains || []).join(', ');
    if (el('emailDirectConsent')) el('emailDirectConsent').value = (settings.direct_consent || []).join(', ');
    if (el('emailCredentialId')) el('emailCredentialId').value = account.id;
    if (el('emailSendFrom')) el('emailSendFrom').value = account.id;
    toggleBackendFields();
    status('emailAccountStatus', 'Editing ' + account.id + '.');
  }

  function newAccount() {
    state.selectedId = '';
    ['emailAccountId', 'emailAccountLabel', 'emailAccountAddress', 'emailImapHost',
      'emailSmtpHost', 'emailSmtpPort', 'emailInternalHost', 'emailInternalPort',
      'emailInternalDomains', 'emailDirectConsent'].forEach(function(id) {
      if (el(id)) el(id).value = '';
    });
    if (el('emailAccountId')) el('emailAccountId').disabled = false;
    if (el('emailAccountBackend')) el('emailAccountBackend').value = '';
    if (el('emailSmtpStarttls')) el('emailSmtpStarttls').checked = true;
    if (el('emailMorningPull')) el('emailMorningPull').checked = true;
    if (el('emailInternalStarttls')) el('emailInternalStarttls').checked = false;
    toggleBackendFields();
    status('emailAccountStatus', 'Creating a new mailbox.');
  }

  async function saveAccount() {
    var account = collectAccount();
    if (!account.id || !account.address) {
      status('emailAccountStatus', 'An account id and address are required.', true);
      return;
    }
    if (account.backend === 'eva_direct') {
      if (!account.settings.internal_domains.length) {
        status('emailAccountStatus', 'List at least one local domain, such as localhost.localdomain.', true);
        return;
      }
      if (!account.settings.direct_consent.length) {
        status('emailAccountStatus', 'List at least one recipient that accepts mail from Eva.', true);
        return;
      }
    }
    try {
      await bridge('/v1/email/account', {
        method: 'POST',
        body: JSON.stringify({ account: account })
      });
      state.selectedId = account.id;
      await refresh();
      loadAccount(account.id);
      status('emailAccountStatus', 'Saved ' + account.id + '.');
    } catch (error) {
      status('emailAccountStatus', 'Rejected: ' + (error && error.message), true);
    }
  }

  async function removeAccount() {
    var id = state.selectedId || (el('emailAccountId') || {}).value;
    if (!id) {
      status('emailAccountStatus', 'Enter the account id to remove.', true);
      return;
    }
    if (typeof confirm === 'function' && !confirm('Remove mailbox ' + id + '?')) return;
    try {
      await bridge('/v1/email/accounts/' + encodeURIComponent(id), { method: 'DELETE' });
      newAccount();
      await refresh();
      status('emailAccountStatus', 'Removed ' + id + '.');
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
      await bridge('/v1/email/allowlist', {
        method: 'POST',
        body: JSON.stringify({ allowlist: splitList(input ? input.value : '') })
      });
      if (input) delete input.dataset.dirty;
      await refresh();
      status('emailAccountStatus', 'Approved recipients updated.');
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
      ['emailNewAccount', newAccount],
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
    var list = el('emailAccountList');
    if (list && !list.dataset.bound) {
      list.addEventListener('click', function(event) {
        var button = event.target.closest('[data-email-edit]');
        if (button) loadAccount(button.getAttribute('data-email-edit'));
      });
      list.dataset.bound = '1';
    }
  }

  function open() {
    var settingsButton = el('evaSettingsBtn');
    if (settingsButton && !document.body.classList.contains('settings-open')) {
      settingsButton.click();
    }
    var tab = document.querySelector('.settings-tab[data-stab="email"]');
    if (tab && !tab.classList.contains('active')) tab.click();
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
    accounts: function() { return state.accounts.slice(); },
    loadAccount: loadAccount
  };
}());
