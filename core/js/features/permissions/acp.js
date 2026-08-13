// Standalone ACP permission polling, rendering, and one-time decisions.
var _acpPermissionState = {
  shown: {},
  polling: false,
  idleIntervalMs: 300000,
  requestIntervalMs: 30000,
  pendingIntervalMs: 3000,
  activeUntil: 0,
  pending: false,
  timer: null
};

function _acpPermissionPollDelay() {
  if (_acpPermissionState.pending) return _acpPermissionState.pendingIntervalMs;
  return Date.now() < _acpPermissionState.activeUntil
    ? _acpPermissionState.requestIntervalMs : _acpPermissionState.idleIntervalMs;
}

function _scheduleACPPermissionPoll(delay) {
  if (!isEvaStandalone()) return;
  if (_acpPermissionState.timer) clearTimeout(_acpPermissionState.timer);
  _acpPermissionState.timer = setTimeout(function() {
    _acpPermissionState.timer = null;
    pollACPPermissions();
  }, typeof delay === 'number' ? delay : _acpPermissionPollDelay());
}

function watchACPPermissions(durationMs) {
  if (!isEvaStandalone()) return;
  var duration = Math.max(0, Number(durationMs) || 0);
  _acpPermissionState.activeUntil = Math.max(_acpPermissionState.activeUntil, Date.now() + duration);
  _scheduleACPPermissionPoll(0);
}

function _resolveACPPermission(permissionId, decision, bubble) {
  backgroundBridgeRequest('/v1/acp/permissions/' + encodeURIComponent(permissionId), {
    method: 'POST',
    headers: getBridgeCapabilityHeaders(),
    body: JSON.stringify({ decision: decision })
  }).then(function() {
    if (bubble && bubble.parentNode) bubble.parentNode.removeChild(bubble);
    _scheduleACPPermissionPoll(0);
  }).catch(function(error) {
    var message = document.createElement('div');
    message.className = 'error';
    message.textContent = error.message || 'Permission could not be resolved; no command was run.';
    if (bubble) bubble.appendChild(message);
    setStatus('error', message.textContent);
  });
}

function _renderACPPermission(permission) {
  if (!permission || _acpPermissionState.shown[permission.id]) return;
  _acpPermissionState.shown[permission.id] = true;
  var output = document.getElementById('txtOutput');
  if (!output) return;
  var bubble = document.createElement('div');
  bubble.className = 'chat-bubble eva-bubble';
  var text = document.createElement('div');
  text.className = 'md';
  var commandSummary = permission.command_summary ? ' Command: ' + String(permission.command_summary) : '';
  text.textContent = 'Allow this ' + String(permission.tool_kind || 'tool') + ' action once?' + commandSummary;
  bubble.appendChild(text);
  var actions = document.createElement('div');
  actions.className = 'background-actions';
  var allow = (permission.options || []).filter(function(option) { return option.kind === 'allow_once'; })[0];
  var allowButton = document.createElement('button');
  allowButton.type = 'button';
  allowButton.className = 'auth-toggle';
  allowButton.textContent = 'Allow once';
  allowButton.disabled = !allow || permission.approval_allowed === false;
  allowButton.addEventListener('click', function() { _resolveACPPermission(permission.id, 'allow', bubble); });
  var rejectButton = document.createElement('button');
  rejectButton.type = 'button';
  rejectButton.className = 'auth-toggle';
  rejectButton.textContent = 'Reject';
  rejectButton.addEventListener('click', function() { _resolveACPPermission(permission.id, 'reject', bubble); });
  actions.appendChild(allowButton);
  actions.appendChild(rejectButton);
  bubble.appendChild(actions);
  output.appendChild(bubble);
  output.scrollTop = output.scrollHeight;
}

function pollACPPermissions() {
  if (_acpPermissionState.polling || !isEvaStandalone()) return Promise.resolve();
  _acpPermissionState.polling = true;
  return backgroundBridgeRequest('/v1/acp/permissions', { headers: getBridgeCapabilityHeaders() })
    .then(function(data) {
      var permissions = data && Array.isArray(data.permissions) ? data.permissions : [];
      _acpPermissionState.pending = permissions.length > 0;
      if (permissions.length) {
        _acpPermissionState.activeUntil = Math.max(_acpPermissionState.activeUntil, Date.now() + 60000);
      }
      permissions.forEach(_renderACPPermission);
    })
    .catch(function() {})
    .finally(function() {
      _acpPermissionState.polling = false;
      _scheduleACPPermissionPoll();
    });
}

function initACPPermissions() {
  if (_acpPermissionState.timer) return;
  _scheduleACPPermissionPoll(1500);
}