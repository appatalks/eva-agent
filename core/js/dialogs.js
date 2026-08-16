// Small in-app replacement for window.prompt(), which Electron disables.

var _evaTextPromptResolve = null;
var _evaActionConfirmResolve = null;

function evaConfirmAction(options) {
  _bindEvaActionConfirm();
  options = options || {};
  var dialog = document.getElementById('evaActionConfirm');
  var title = document.getElementById('evaActionConfirmTitle');
  var warning = document.getElementById('evaActionConfirmWarning');
  var details = document.getElementById('evaActionConfirmDetails');
  var approve = document.getElementById('evaActionConfirmApprove');
  if (!dialog || !title || !warning || !details || !approve) return Promise.resolve(false);
  if (_evaActionConfirmResolve) _evaActionConfirmResolve(false);
  title.textContent = String(options.title || 'Confirm action');
  warning.textContent = String(options.warning || 'Review this action before approving.');
  details.textContent = String(options.details || '');
  approve.textContent = String(options.confirmLabel || 'Confirm');
  dialog.setAttribute('aria-hidden', 'false');
  return new Promise(function(resolve) {
    _evaActionConfirmResolve = resolve;
    requestAnimationFrame(function() { approve.focus(); });
  });
}

function _closeEvaActionConfirm(approved) {
  var dialog = document.getElementById('evaActionConfirm');
  if (dialog) dialog.setAttribute('aria-hidden', 'true');
  var resolve = _evaActionConfirmResolve;
  _evaActionConfirmResolve = null;
  if (resolve) resolve(approved === true);
}

function _bindEvaActionConfirm() {
  var dialog = document.getElementById('evaActionConfirm');
  if (!dialog || dialog.dataset.bound === 'true') return;
  dialog.dataset.bound = 'true';
  var approve = document.getElementById('evaActionConfirmApprove');
  var cancel = document.getElementById('evaActionConfirmCancel');
  if (approve) approve.addEventListener('click', function() { _closeEvaActionConfirm(true); });
  if (cancel) cancel.addEventListener('click', function() { _closeEvaActionConfirm(false); });
  dialog.addEventListener('click', function(event) {
    if (event.target === dialog) _closeEvaActionConfirm(false);
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape' && dialog.getAttribute('aria-hidden') === 'false') {
      event.preventDefault();
      _closeEvaActionConfirm(false);
    }
  });
}

function evaTextPrompt(title, initialValue, options) {
  _bindEvaTextPrompt();
  options = options || {};
  var dialog = document.getElementById('evaTextPrompt');
  var titleEl = document.getElementById('evaTextPromptTitle');
  var input = document.getElementById('evaTextPromptInput');
  if (!dialog || !titleEl || !input) return Promise.resolve(null);
  if (_evaTextPromptResolve) _evaTextPromptResolve(null);
  titleEl.textContent = String(title || 'Enter a value');
  input.value = String(initialValue || '');
  input.maxLength = Math.max(1, Number(options.maxLength) || 120);
  input.placeholder = String(options.placeholder || '');
  dialog.dataset.promptKind = options.kind || (/github repository url/i.test(titleEl.textContent) ? 'github_repository_url' : 'text');
  dialog.setAttribute('aria-hidden', 'false');
  return new Promise(function(resolve) {
    _evaTextPromptResolve = resolve;
    requestAnimationFrame(function() {
      input.focus();
      input.select();
    });
  });
}

function _closeEvaTextPrompt(value) {
  var dialog = document.getElementById('evaTextPrompt');
  if (dialog) dialog.setAttribute('aria-hidden', 'true');
  var resolve = _evaTextPromptResolve;
  _evaTextPromptResolve = null;
  if (resolve) resolve(value);
}

function evaTextPromptIsOpen() {
  var dialog = document.getElementById('evaTextPrompt');
  return !!(_evaTextPromptResolve && dialog && dialog.getAttribute('aria-hidden') === 'false');
}

function evaTextPromptCancel() {
  if (!evaTextPromptIsOpen()) return false;
  _closeEvaTextPrompt(null);
  return true;
}

function evaTextPromptDescribe() {
  if (!evaTextPromptIsOpen()) return { open: false, form: 'text_prompt', fields: [] };
  var dialog = document.getElementById('evaTextPrompt');
  var title = document.getElementById('evaTextPromptTitle');
  var input = document.getElementById('evaTextPromptInput');
  var kind = dialog.dataset.promptKind || 'text';
  return {
    open: true,
    form: 'text_prompt',
    title: title ? title.textContent : '',
    fields: [{
      id: kind,
      type: kind === 'github_repository_url' ? 'url' : 'text',
      required: true,
      value: input ? input.value : '',
      maxLength: input ? input.maxLength : 120,
      format: kind === 'github_repository_url' ? 'https://github.com/owner/repository' : ''
    }],
    actions: ['set_field', 'submit_form', 'cancel_form']
  };
}

function evaTextPromptSetField(fieldId, value) {
  var schema = evaTextPromptDescribe();
  if (!schema.open || !schema.fields.length) return { ok: false, message: 'No native text prompt is open.' };
  var field = schema.fields[0];
  if (String(fieldId || '') !== field.id) return { ok: false, message: 'Unknown native field: ' + String(fieldId || '') };
  var normalized = String(value || '').trim();
  if (field.id === 'github_repository_url') {
    normalized = _evaGithubPromptUrl(_normalizeEvaPromptVoiceValue(normalized));
    if (!normalized) return { ok: false, message: 'Expected https://github.com/owner/repository.' };
  }
  if (!normalized || normalized.length > field.maxLength) return { ok: false, message: 'Native field value is missing or too long.' };
  var input = document.getElementById('evaTextPromptInput');
  input.value = normalized;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  var label = field.id === 'github_repository_url' ? 'GitHub repository URL' : field.id;
  return { ok: true, field: field.id, value: normalized, message: 'Set ' + label + '.' };
}

function evaTextPromptSubmit(fieldId) {
  var schema = evaTextPromptDescribe();
  if (!schema.open || !schema.fields.length) return { ok: false, message: 'No native text prompt is open.' };
  var field = schema.fields[0];
  if (fieldId && String(fieldId) !== field.id) return { ok: false, message: 'Unknown native field: ' + String(fieldId) };
  var checked = evaTextPromptSetField(field.id, field.value);
  if (!checked.ok) return checked;
  var form = document.getElementById('evaTextPromptForm');
  if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
  else _closeEvaTextPrompt(checked.value);
  var label = field.id === 'github_repository_url' ? 'GitHub repository URL' : field.id;
  return { ok: true, field: field.id, value: checked.value, message: 'Submitted ' + label + '.' };
}

function _normalizeEvaPromptVoiceValue(value) {
  var text = String(value || '').trim();
  var dialog = document.getElementById('evaTextPrompt');
  if (!dialog || dialog.dataset.promptKind !== 'github_repository_url') return text;
  text = text.replace(/^(?:the\s+)?(?:url|link|repository|repo)(?:\s+is)?\s+/i, '');
  text = text.replace(/^use\s+/i, '');
  text = text.replace(/https?\s*(?:colon|:)\s*(?:slash|\/)\s*(?:slash|\/)/i, 'https://');
  text = text.replace(/github\s+(?:dot|\.)\s+com/i, 'github.com');
  text = text.replace(/\s+(?:forward\s+)?slash\s+/gi, '/');
  text = text.replace(/\s+(?:dot|\.)\s+git\b/i, '.git');
  text = text.replace(/\bapatox\b/gi, 'appatalks');
  var direct = text.match(/https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?/i);
  if (direct) return direct[0];
  var githubPath = text.match(/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?/i);
  return githubPath ? 'https://' + githubPath[0] : text;
}

function _evaGithubPromptUrl(value) {
  var match = String(value || '').trim().match(/^https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?$/i);
  if (!match) return '';
  var owner = match[1].toLowerCase() === 'apatox' ? 'appatalks' : match[1];
  return 'https://github.com/' + owner + '/' + match[2].replace(/\.git$/i, '');
}

function _evaGithubSlug(value) {
  var slug = String(value || '').trim()
    .replace(/^(?:is\s+)?(?:spelled|spelt|spell(?:ed)?(?:\s+as)?)\s+/i, '')
    .replace(/\b(?:[A-Za-z0-9]\s+){2,}[A-Za-z0-9]\b/g, function(spelling) { return spelling.replace(/\s+/g, ''); })
    .replace(/\b(?:dash|hyphen)\b/gi, '-')
    .replace(/\b(?:dot|period)\b/gi, '.')
    .replace(/["'`]/g, '')
    .replace(/[^A-Za-z0-9_. -]+$/g, '')
    .replace(/\s*([._-])\s*/g, '$1')
    .replace(/\s+/g, '-');
  return /^[A-Za-z0-9_.-]+$/.test(slug) ? slug : '';
}

function _evaGithubPromptCorrection(value, currentValue) {
  var current = _evaGithubPromptUrl(currentValue);
  if (!current) return '';
  var parts = current.replace('https://github.com/', '').split('/');
  var text = String(value || '').trim().replace(/^\s*(?:eva|ava)[,.:]?\s*/i, '');
  var owner = text.match(/\bowner(?:\s+name)?\s+(?:is|to|as|should be)\s+(.+)$/i);
  if (owner) {
    var ownerSlug = _evaGithubSlug(owner[1]);
    return ownerSlug ? 'https://github.com/' + ownerSlug + '/' + parts[1] : '';
  }
  var repository = text.match(/\b(?:my\s+)?(?:repo|repository)(?:\s+name)?\s+(?:is\s+)?(?:spelled|spelt)\s+(.+)$/i)
    || text.match(/\b(?:repo|repository)(?:\s+name)?\s+(?:is called|is|to|as|should be)\s+(.+)$/i)
    || text.match(/\b(?:correct|change|replace|set|spell)(?:\s+(?:the\s+)?(?:repo|repository|name|it))?\s+(?:to|as)\s+(.+)$/i)
    || text.match(/\b(?:it should be|it is|it's|no[, ]+(?:it is|it's|use))\s+(.+)$/i);
  if (repository) {
    var repositorySlug = _evaGithubSlug(repository[1]);
    return repositorySlug ? 'https://github.com/' + parts[0] + '/' + repositorySlug : '';
  }
  var pair = text.match(/^(?:use\s+)?([A-Za-z0-9_. -]+?)\s*(?:forward\s+slash|slash|\/)\s*([A-Za-z0-9_. -]+)$/i);
  if (!pair || /\b(?:repo|repository|name|spell|spelled|spelt)\b/i.test(pair[1])) return '';
  var pairOwner = _evaGithubSlug(pair[1]);
  var pairRepository = _evaGithubSlug(pair[2]);
  return pairOwner && pairRepository ? 'https://github.com/' + pairOwner + '/' + pairRepository : '';
}

function evaTextPromptConsumeVoice(value) {
  if (!evaTextPromptIsOpen()) return false;
  var text = String(value || '').trim();
  if (/^(?:cancel|never mind|nevermind|stop)$/i.test(text)) {
    _closeEvaTextPrompt(null);
    return true;
  }
  var title = document.getElementById('evaTextPromptTitle');
  var dialog = document.getElementById('evaTextPrompt');
  var githubPrompt = !!(dialog && dialog.dataset.promptKind === 'github_repository_url');
  text = _normalizeEvaPromptVoiceValue(text);
  if (githubPrompt) {
    var inputValue = (document.getElementById('evaTextPromptInput') || {}).value || '';
    text = _evaGithubPromptUrl(text) || _evaGithubPromptCorrection(value, inputValue);
    if (!text) {
      if (/\b(?:misspelled|misspelt|spelled wrong|wrong|incorrect|fix it|correct it|try again)\b/i.test(value)) {
        title.textContent = 'Say the corrected repository name, owner slash repository, or full GitHub URL';
        var currentInput = document.getElementById('evaTextPromptInput');
        if (currentInput) { currentInput.focus(); currentInput.select(); }
        return true;
      }
      return false;
    }
  }
  if (!text) return false;
  var input = document.getElementById('evaTextPromptInput');
  var form = document.getElementById('evaTextPromptForm');
  if (input) {
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  setTimeout(function() {
    if (!evaTextPromptIsOpen()) return;
    if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
    else _closeEvaTextPrompt(text);
  }, 160);
  return true;
}

function _bindEvaTextPrompt() {
  var form = document.getElementById('evaTextPromptForm');
  var input = document.getElementById('evaTextPromptInput');
  var cancel = document.getElementById('evaTextPromptCancel');
  var dialog = document.getElementById('evaTextPrompt');
  if (!dialog || dialog.dataset.bound === 'true') return;
  dialog.dataset.bound = 'true';
  if (form) form.addEventListener('submit', function(event) {
    event.preventDefault();
    _closeEvaTextPrompt(input ? input.value : '');
  });
  if (cancel) cancel.addEventListener('click', function() { _closeEvaTextPrompt(null); });
  if (dialog) dialog.addEventListener('click', function(event) {
    if (event.target === dialog) _closeEvaTextPrompt(null);
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape' && dialog && dialog.getAttribute('aria-hidden') === 'false') {
      event.preventDefault();
      _closeEvaTextPrompt(null);
    }
  });
}

document.addEventListener('DOMContentLoaded', function() {
  _bindEvaTextPrompt();
  _bindEvaActionConfirm();
});