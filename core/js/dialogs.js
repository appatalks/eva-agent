// Small in-app replacement for window.prompt(), which Electron disables.

var _evaTextPromptResolve = null;

function evaTextPrompt(title, initialValue, options) {
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

document.addEventListener('DOMContentLoaded', function() {
  var form = document.getElementById('evaTextPromptForm');
  var input = document.getElementById('evaTextPromptInput');
  var cancel = document.getElementById('evaTextPromptCancel');
  var dialog = document.getElementById('evaTextPrompt');
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
});