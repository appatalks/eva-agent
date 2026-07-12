// Javascript for Options
// 

// Global Variables
var lastResponse = "";
var userMasterResponse = "";
var aiMasterResponse = "";
var masterOutput = "";
var storageAssistant = "";
var imgSrcGlobal; // Declare a global variable for img.src
// Debug/CORS flags (from config.json)
var DEBUG_CORS = false;
var DEBUG_PROXY_URL = "";

// Error Handling Variables
var retryCount = 0;
var maxRetries = 5;
var retryDelay = 2420; // milliseconds

// API Access[OpenAI, AWS] 
function auth() {
  // Prefer inlined local config if provided (config.local.js)
  if (typeof window !== 'undefined' && window.__LOCAL_CONFIG__) {
    const config = window.__LOCAL_CONFIG__;
    applyConfig(config);
    return;
  }

  // Fallback: fetch config.json (requires http(s) server)
  if (location.protocol === 'file:') {
    console.warn('Running from file://, unable to fetch config.json due to browser security. Create config.local.js or serve over http.');
  }

  fetch('./config.json')
    .then(response => response.json())
    .then(config => applyConfig(config))
    .catch(err => {
      console.error('Failed to load config:', err);
      document.getElementById('idText').innerText = 'Config not loaded. Use config.local.js or run a local server.';
    });
}

function applyConfig(config) {
  OPENAI_API_KEY = config.OPENAI_API_KEY;
  // Google Gemini key if provided
  GOOGLE_GL_KEY = config.GOOGLE_GL_KEY;
  GOOGLE_VISION_KEY = config.GOOGLE_VISION_KEY;
  // GitHub Copilot PAT
  if (config.GITHUB_PAT) GITHUB_PAT = config.GITHUB_PAT;
  // CORS debug
  DEBUG_CORS = !!config.DEBUG_CORS;
  DEBUG_PROXY_URL = config.DEBUG_PROXY_URL || "";
  AWS.config.region = config.AWS_REGION;
  AWS.config.credentials = new AWS.Credentials(config.AWS_ACCESS_KEY_ID, config.AWS_SECRET_ACCESS_KEY);
  // Apply any localStorage auth overrides
  loadAuthOverrides();
}

// --- Auth Key Management ---
function getAuthKey(key) {
  var stored = localStorage.getItem('auth_' + key);
  if (stored) return stored;
  if (typeof window[key] !== 'undefined') return window[key];
  return '';
}

function loadAuthOverrides() {
  var keys = ['OPENAI_API_KEY', 'GOOGLE_GL_KEY', 'GOOGLE_VISION_KEY', 'GITHUB_PAT'];
  keys.forEach(function(key) {
    var val = localStorage.getItem('auth_' + key);
    if (val) window[key] = val;
  });
}

function saveAuthKeys() {
  var map = {
    'authOpenAI': 'OPENAI_API_KEY',
    'authGitHub': 'GITHUB_PAT',
    'authGemini': 'GOOGLE_GL_KEY',
    'authGoogleVision': 'GOOGLE_VISION_KEY'
  };
  Object.keys(map).forEach(function(fieldId) {
    var el = document.getElementById(fieldId);
    var key = map[fieldId];
    if (el && el.value.trim()) {
      localStorage.setItem('auth_' + key, el.value.trim());
      window[key] = el.value.trim();
    } else if (el) {
      localStorage.removeItem('auth_' + key);
    }
  });
  // Save ACP Bridge URL separately
  var acpEl = document.getElementById('txtACPBridgeUrl');
  if (acpEl && typeof isEvaStandalone === 'function' && isEvaStandalone()) {
    localStorage.removeItem('acp_bridge_url');
  } else if (acpEl && acpEl.value.trim()) {
    localStorage.setItem('acp_bridge_url', acpEl.value.trim());
  } else if (acpEl) {
    localStorage.removeItem('acp_bridge_url');
  }
  var lmsBaseEl = document.getElementById('aigLmStudioBaseUrl');
  if (lmsBaseEl && lmsBaseEl.value.trim()) {
    localStorage.setItem('aig_lmstudio_base_url', lmsBaseEl.value.trim());
  } else if (lmsBaseEl) {
    localStorage.removeItem('aig_lmstudio_base_url');
  }
  var lmsModelEl = document.getElementById('aigLmStudioModel');
  if (lmsModelEl && lmsModelEl.value.trim()) {
    localStorage.setItem('aig_lmstudio_model', lmsModelEl.value.trim());
  } else if (lmsModelEl) {
    localStorage.removeItem('aig_lmstudio_model');
  }
  // Save Signal sender/recipient to localStorage and push to bridge
  var sigSender = document.getElementById('authSignalSender');
  var sigRecip = document.getElementById('authSignalRecipient');
  if (sigSender && sigSender.value.trim()) {
    localStorage.setItem('signal_sender', sigSender.value.trim());
  } else if (sigSender) {
    localStorage.removeItem('signal_sender');
  }
  if (sigRecip && sigRecip.value.trim()) {
    localStorage.setItem('signal_recipient', sigRecip.value.trim());
  } else if (sigRecip) {
    localStorage.removeItem('signal_recipient');
  }
  _pushSignalSettingsToBridge();
  if (typeof _acpBridgeCache !== 'undefined') _acpBridgeCache = null;
  if (typeof loadGoals === 'function') loadGoals(true);
  if (typeof loadBackgroundData === 'function') loadBackgroundData(true);
  setStatus('info', 'API keys saved to browser storage.');
}

function _pushSignalSettingsToBridge() {
  var sender = (localStorage.getItem('signal_sender') || '').trim();
  var recipient = (localStorage.getItem('signal_recipient') || '').trim();
  if (!sender && !recipient) return;
  var bUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', bUrl + '/v1/alerts/settings', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.send(JSON.stringify({
      signal_sender: sender,
      signal_recipient: recipient
    }));
  } catch (e) { /* bridge may not be running */ }
}

function _hydrateSignalFromBridge(senderEl, recipEl) {
  var bUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', bUrl + '/v1/alerts', true);
    xhr.onload = function() {
      if (xhr.status !== 200) return;
      try {
        var data = JSON.parse(xhr.responseText);
        var s = (data.settings || {}).signal_sender || '';
        var r = (data.settings || {}).signal_recipient || '';
        if (s && senderEl && !senderEl.value) {
          senderEl.value = s;
          localStorage.setItem('signal_sender', s);
        }
        if (r && recipEl && !recipEl.value) {
          recipEl.value = r;
          localStorage.setItem('signal_recipient', r);
        }
      } catch (e) { /* parse error */ }
    };
    xhr.send();
  } catch (e) { /* bridge may not be running */ }
}

function populateAuthFields() {
  var map = {
    'authOpenAI': 'OPENAI_API_KEY',
    'authGitHub': 'GITHUB_PAT',
    'authGemini': 'GOOGLE_GL_KEY',
    'authGoogleVision': 'GOOGLE_VISION_KEY'
  };
  Object.keys(map).forEach(function(fieldId) {
    var el = document.getElementById(fieldId);
    var key = map[fieldId];
    if (el) {
      var val = localStorage.getItem('auth_' + key) || (typeof window[key] !== 'undefined' ? window[key] : '');
      el.value = val || '';
    }
  });
  // Populate ACP Bridge URL
  var acpEl = document.getElementById('txtACPBridgeUrl');
  if (acpEl) {
    acpEl.value = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : (localStorage.getItem('acp_bridge_url') || 'http://localhost:8888');
  }
  var lmsBaseEl = document.getElementById('aigLmStudioBaseUrl');
  if (lmsBaseEl) {
    lmsBaseEl.value = (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : (localStorage.getItem('aig_lmstudio_base_url') || 'http://localhost:1234/v1');
  }
  var lmsModelEl = document.getElementById('aigLmStudioModel');
  if (lmsModelEl) {
    lmsModelEl.value = (typeof getLmStudioModel === 'function') ? getLmStudioModel() : (localStorage.getItem('aig_lmstudio_model') || 'granite-3.1-8b-instruct');
  }
  // Signal fields: prefer localStorage, but if empty, hydrate from the bridge
  // (the bridge persists numbers in alerts.json, which survives AppImage rebuilds).
  var sigSender = document.getElementById('authSignalSender');
  if (sigSender) sigSender.value = localStorage.getItem('signal_sender') || '';
  var sigRecip = document.getElementById('authSignalRecipient');
  if (sigRecip) sigRecip.value = localStorage.getItem('signal_recipient') || '';
  if ((!sigSender || !sigSender.value) || (!sigRecip || !sigRecip.value)) {
    _hydrateSignalFromBridge(sigSender, sigRecip);
  }
}

function getLmStudioBaseUrl() {
  var v = (localStorage.getItem('aig_lmstudio_base_url') || '').trim();
  return v || 'http://localhost:1234/v1';
}

function getLmStudioModel() {
  var v = (localStorage.getItem('aig_lmstudio_model') || '').trim();
  return v || 'granite-3.1-8b-instruct';
}

function getSafeBridgeBaseUrl() {
  var fallback = 'http://localhost:8888';
  var raw = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : fallback;
  try {
    var parsed = new URL(raw || fallback);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return fallback;
    }
    return (parsed.origin + parsed.pathname).replace(/\/+$/, '');
  } catch (e) {
    return fallback;
  }
}

async function getSettingsBridgeUrl() {
  if (typeof detectACPBridge === 'function') {
    return await detectACPBridge();
  }
  if (typeof getACPBridgeUrl === 'function') {
    return getACPBridgeUrl();
  }
  return 'http://localhost:8888';
}

var _goalsState = { goals: [], loading: false, emptyMessage: '' };

async function getGoalsBridgeUrl() {
  return await getSettingsBridgeUrl();
}

function getGoalField(goal, primary, alternate) {
  if (!goal) return '';
  if (goal[primary] !== undefined && goal[primary] !== null) return goal[primary];
  if (alternate && goal[alternate] !== undefined && goal[alternate] !== null) return goal[alternate];
  return '';
}

function formatGoalCategory(value) {
  return String(value || '').replace(/_/g, ' ') || 'uncategorized';
}

function formatGoalDate(value) {
  if (!value) return '-';
  var date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function setGoalsStatus(type, text, quiet) {
  var statusEl = document.getElementById('goalsStatus');
  if (statusEl) {
    statusEl.textContent = text || '';
    statusEl.setAttribute('data-status', type || 'info');
  }
  if (text && !quiet) {
    setStatus(type === 'error' ? 'error' : 'info', text);
  }
}

function updateActiveGoalsCount(goals) {
  var countEl = document.getElementById('evaActiveGoalsCount');
  if (!countEl) return;
  var active = (goals || []).filter(function(goal) {
    return String(getGoalField(goal, 'Status', 'status') || '').toLowerCase() === 'active';
  }).length;
  countEl.textContent = String(active);
}

function renderGoalsList() {
  var listEl = document.getElementById('goalsList');
  if (!listEl) return;
  listEl.innerHTML = '';

  if (_goalsState.emptyMessage) {
    var empty = document.createElement('div');
    empty.className = 'auth-note';
    empty.textContent = _goalsState.emptyMessage;
    listEl.appendChild(empty);
    return;
  }

  if (!_goalsState.goals.length) {
    var none = document.createElement('div');
    none.className = 'auth-note';
    none.textContent = 'No goals yet.';
    listEl.appendChild(none);
    return;
  }

  _goalsState.goals.forEach(function(goal) {
    var goalId = String(getGoalField(goal, 'GoalId', 'goalId') || '');
    var title = String(getGoalField(goal, 'Title', 'title') || 'Untitled goal');
    var description = String(getGoalField(goal, 'Description', 'description') || '');
    var category = String(getGoalField(goal, 'Category', 'category') || '');
    var priority = getGoalField(goal, 'Priority', 'priority');
    var status = String(getGoalField(goal, 'Status', 'status') || '');
    var updatedAt = getGoalField(goal, 'UpdatedAt', 'updatedAt');

    var row = document.createElement('div');
    row.className = 'goal-row';

    var head = document.createElement('div');
    head.className = 'goal-row-head';

    var titleEl = document.createElement('div');
    titleEl.className = 'goal-title';
    titleEl.textContent = title;
    head.appendChild(titleEl);

    var actions = document.createElement('div');
    actions.className = 'goal-actions';
    var editButton = document.createElement('button');
    editButton.type = 'button';
    editButton.className = 'auth-toggle';
    editButton.textContent = 'Edit';
    editButton.addEventListener('click', function() { openGoalForm(goal); });
    actions.appendChild(editButton);
    var deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'auth-toggle';
    deleteButton.textContent = 'Delete';
    deleteButton.addEventListener('click', function() { deleteGoal(goalId, title); });
    actions.appendChild(deleteButton);
    head.appendChild(actions);

    var meta = document.createElement('div');
    meta.className = 'goal-meta';
    var categoryBadge = document.createElement('span');
    categoryBadge.className = 'goal-badge';
    categoryBadge.textContent = formatGoalCategory(category);
    meta.appendChild(categoryBadge);
    var priorityEl = document.createElement('span');
    priorityEl.textContent = 'Priority: ' + (priority === '' ? '-' : priority);
    meta.appendChild(priorityEl);
    var statusEl = document.createElement('span');
    statusEl.textContent = 'Status: ' + (status || '-');
    meta.appendChild(statusEl);
    var updatedEl = document.createElement('span');
    updatedEl.textContent = 'Updated: ' + formatGoalDate(updatedAt);
    meta.appendChild(updatedEl);

    row.appendChild(head);
    row.appendChild(meta);
    if (description) {
      var desc = document.createElement('div');
      desc.className = 'goal-description';
      desc.textContent = description;
      row.appendChild(desc);
    }
    listEl.appendChild(row);
  });
}

async function goalsBridgeRequest(path, options) {
  options = _withBridgeMutationEnvelope(options || {});
  var bridgeUrl = await getGoalsBridgeUrl();
  var response = await fetch(bridgeUrl.replace(/\/+$/, '') + path, options || {});
  var text = await response.text();
  var data = {};
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
  }
  if (!response.ok) {
    var message = data && data.error && data.error.message ? data.error.message : (data.message || ('HTTP ' + response.status));
    var error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

async function loadGoals(quiet) {
  if (_goalsState.loading) return;
  _goalsState.loading = true;
  setGoalsStatus('info', quiet ? '' : 'Loading goals...', true);
  try {
    var options = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
      options.signal = AbortSignal.timeout(3000);
    }
    var data = await goalsBridgeRequest('/v1/goals', options);
    _goalsState.goals = Array.isArray(data.goals) ? data.goals : [];
    _goalsState.emptyMessage = '';
    updateActiveGoalsCount(_goalsState.goals);
    renderGoalsList();
    setGoalsStatus('info', _goalsState.goals.length ? '' : 'No goals yet.', true);
  } catch (error) {
    _goalsState.goals = [];
    updateActiveGoalsCount([]);
    if (error && error.status === 503) {
      _goalsState.emptyMessage = error.message || 'Goals are not available right now.';
      setGoalsStatus('warn', _goalsState.emptyMessage, true);
    } else {
      _goalsState.emptyMessage = 'Goals are not available right now.';
      setGoalsStatus('error', 'Goals load failed: ' + (error.message || error), quiet);
    }
    renderGoalsList();
  } finally {
    _goalsState.loading = false;
  }
}

function setGoalFormStatusMode(isEdit) {
  var statusField = document.getElementById('goalStatusField');
  if (statusField) statusField.style.display = isEdit ? 'block' : 'none';
}

function openGoalForm(goal) {
  var form = document.getElementById('goalsForm');
  if (!form) return;
  var isEdit = !!goal;
  var goalId = isEdit ? String(getGoalField(goal, 'GoalId', 'goalId') || '') : '';
  var editId = document.getElementById('goalEditId');
  var title = document.getElementById('goalTitle');
  var description = document.getElementById('goalDescription');
  var category = document.getElementById('goalCategory');
  var priority = document.getElementById('goalPriority');
  var status = document.getElementById('goalStatusSelect');
  if (editId) editId.value = goalId;
  if (title) title.value = isEdit ? String(getGoalField(goal, 'Title', 'title') || '') : '';
  if (description) description.value = isEdit ? String(getGoalField(goal, 'Description', 'description') || '') : '';
  if (category) category.value = isEdit ? String(getGoalField(goal, 'Category', 'category') || 'relational') : 'relational';
  if (priority) priority.value = isEdit ? String(getGoalField(goal, 'Priority', 'priority') || 50) : '50';
  if (status) status.value = isEdit ? String(getGoalField(goal, 'Status', 'status') || 'active') : 'active';
  setGoalFormStatusMode(isEdit);
  form.style.display = 'block';
  setGoalsStatus('info', '', true);
  if (title) title.focus();
}

function closeGoalForm() {
  var form = document.getElementById('goalsForm');
  if (form) form.style.display = 'none';
  setGoalFormStatusMode(false);
}

function readGoalForm() {
  var title = document.getElementById('goalTitle');
  var description = document.getElementById('goalDescription');
  var category = document.getElementById('goalCategory');
  var priority = document.getElementById('goalPriority');
  var status = document.getElementById('goalStatusSelect');
  var editId = document.getElementById('goalEditId');
  var titleValue = title ? title.value.trim() : '';
  var descriptionValue = description ? description.value.trim() : '';
  var priorityValue = priority ? Number(priority.value) : NaN;
  if (!titleValue) return { error: 'Title is required.' };
  if (titleValue.length > 200) return { error: 'Title must be 200 characters or fewer.' };
  if (descriptionValue.length > 2000) return { error: 'Description must be 2000 characters or fewer.' };
  if (!Number.isInteger(priorityValue) || priorityValue < 0 || priorityValue > 100) return { error: 'Priority must be an integer from 0 to 100.' };
  var body = {
    title: titleValue,
    description: descriptionValue,
    category: category ? category.value : 'relational',
    priority: priorityValue,
    relatedTopics: ''
  };
  if (editId && editId.value && status) body.status = status.value;
  return { body: body, goalId: editId ? editId.value : '' };
}

async function saveGoalFromSettings() {
  var formData = readGoalForm();
  if (formData.error) {
    setGoalsStatus('error', formData.error, false);
    return;
  }
  var isEdit = !!formData.goalId;
  var button = document.getElementById('goalsSaveButton');
  if (button) button.disabled = true;
  setGoalsStatus('info', 'Saving goal...', true);
  try {
    var path = isEdit ? '/v1/goals/' + encodeURIComponent(formData.goalId) : '/v1/goals';
    await goalsBridgeRequest(path, {
      method: isEdit ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(formData.body)
    });
    closeGoalForm();
    await loadGoals(true);
    setGoalsStatus('info', 'Goal saved.', false);
  } catch (error) {
    var message = error && error.status === 503 ? (error.message || 'Goals are not available right now.') : ('Goal save failed: ' + (error.message || error));
    setGoalsStatus('error', message, false);
  } finally {
    if (button) button.disabled = false;
  }
}

async function deleteGoal(goalId, title) {
  if (!goalId) return;
  if (!confirm('Drop goal "' + title + '"?')) return;
  setGoalsStatus('info', 'Dropping goal...', true);
  try {
    var mutationEnvelope = (typeof captureMutationEnvelope === 'function')
      ? captureMutationEnvelope() : null;
    await goalsBridgeRequest('/v1/goals/' + encodeURIComponent(goalId), {
      method: 'DELETE', evaMutationEnvelope: mutationEnvelope
    });
    await loadGoals(true);
    setGoalsStatus('info', 'Goal dropped.', false);
  } catch (error) {
    var message = error && error.status === 503 ? (error.message || 'Goals are not available right now.') : ('Goal delete failed: ' + (error.message || error));
    setGoalsStatus('error', message, false);
  }
}

function initGoals() {
  var newButton = document.getElementById('goalsNewButton');
  var refreshButton = document.getElementById('goalsRefreshButton');
  var saveButton = document.getElementById('goalsSaveButton');
  var cancelButton = document.getElementById('goalsCancelButton');
  if (newButton) newButton.addEventListener('click', function() { openGoalForm(null); });
  if (refreshButton) refreshButton.addEventListener('click', function() { loadGoals(false); });
  if (saveButton) saveButton.addEventListener('click', saveGoalFromSettings);
  if (cancelButton) cancelButton.addEventListener('click', closeGoalForm);
  renderGoalsList();
  loadGoals(true);
}

var _backgroundState = { status: null, proposals: [], activity: [], loading: false, error: '' };

function _withBridgeMutationEnvelope(options) {
  var copy = Object.assign({}, options || {});
  var method = String(copy.method || 'GET').toUpperCase();
  if (method !== 'POST' && method !== 'PATCH' && method !== 'DELETE') return copy;
  var operationEnvelope = copy.evaMutationEnvelope ||
    ((typeof captureMutationEnvelope === 'function') ? captureMutationEnvelope() :
      ((typeof captureOperationEnvelope === 'function') ? captureOperationEnvelope() : null));
  delete copy.evaMutationEnvelope;
  if ((method === 'POST' || method === 'PATCH') && copy.body && operationEnvelope) {
    try {
      var parsed = JSON.parse(copy.body);
      copy.body = JSON.stringify(Object.assign({}, parsed, operationEnvelope));
    } catch (_) {}
  } else if (method === 'DELETE' && operationEnvelope) {
    copy.headers = Object.assign({}, copy.headers || {}, {
      'X-Eva-Request-Id': operationEnvelope.request_id,
      'X-Eva-Correlation-Id': operationEnvelope.correlation_id,
      'X-Eva-Session-Id': operationEnvelope.session_id,
      'X-Eva-Turn-Id': operationEnvelope.turn_id
    });
  }
  return copy;
}

async function backgroundBridgeRequest(path, options) {
  options = _withBridgeMutationEnvelope(options || {});
  var bridgeUrl = await getSettingsBridgeUrl();
  var response = await fetch(bridgeUrl.replace(/\/+$/, '') + path, options || {});
  var text = await response.text();
  var data = {};
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
  }
  if (!response.ok) {
    var message = data && data.error && data.error.message ? data.error.message : (data.message || ('HTTP ' + response.status));
    var error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function getBackgroundField(row, primary, alternate) {
  if (!row) return '';
  if (row[primary] !== undefined && row[primary] !== null) return row[primary];
  if (alternate && row[alternate] !== undefined && row[alternate] !== null) return row[alternate];
  return '';
}

function getBackgroundPayload(proposal) {
  var payload = getBackgroundField(proposal, 'Payload', 'payload');
  if (!payload) return {};
  if (typeof payload === 'string') {
    try { return JSON.parse(payload); } catch (_) { return {}; }
  }
  return typeof payload === 'object' ? payload : {};
}

function renderBackgroundStatus() {
  var statusEl = document.getElementById('backgroundStatus');
  if (!statusEl) return;
  var status = _backgroundState.status || null;
  if (!status) {
    statusEl.textContent = _backgroundState.error || 'Background status unavailable.';
    statusEl.setAttribute('data-status', _backgroundState.error ? 'warn' : 'info');
    return;
  }
  var lastError = _backgroundState.error || status.lastError || 'none';
  var parts = [
    'Running: ' + (status.running ? 'yes' : 'no'),
    'Enabled: ' + (status.enabled ? 'yes' : 'no'),
    'Interval: ' + (status.intervalSeconds || 0) + 's',
    'Last tick: ' + (status.lastTick ? formatGoalDate(status.lastTick) : '-'),
    'Last error: ' + lastError
  ];
  statusEl.textContent = parts.join(' | ');
  statusEl.setAttribute('data-status', lastError === 'none' ? 'info' : 'warn');

  var enabledEl = document.getElementById('backgroundEnabled');
  var intervalEl = document.getElementById('backgroundIntervalSeconds');
  if (enabledEl) enabledEl.checked = !!status.enabled;
  if (intervalEl && status.intervalSeconds) intervalEl.value = String(status.intervalSeconds);

  if (status.jobs && typeof status.jobs === 'object') {
    var jobInputs = document.querySelectorAll('#backgroundJobs input[data-job]');
    Array.prototype.forEach.call(jobInputs, function(input) {
      var jobType = input.getAttribute('data-job');
      if (Object.prototype.hasOwnProperty.call(status.jobs, jobType)) {
        input.checked = !!status.jobs[jobType];
      }
    });
  }
}

function backgroundJobLabel(jobType) {
  switch (String(jobType || '').toLowerCase()) {
    case 'memory_consolidation': return 'Memory summary proposal';
    case 'goal_checkin': return 'Goal check-in';
    case 'daily_digest': return 'Daily digest';
    case 'knowledge_hygiene': return 'Knowledge decay / dedup';
    case 'reflection_synthesis': return 'Reflection synthesis';
    case 'emotion_drift': return 'Emotion baseline drift';
    case 'token_telemetry': return 'Token telemetry';
    case 'proactive_briefing': return 'Proactive briefing';
    case 'market_snapshot': return 'Market snapshot';
    case 'sec_filing_watch': return 'SEC filing watch';
    case 'space_weather_alert': return 'Space weather alert';
    case 'research_deepdive': return 'Research deep-dive';
    case 'alert_watch': return 'Alert watch';
    default: return jobType ? String(jobType) : 'Background proposal';
  }
}

function renderBackgroundProposals() {
  var listEl = document.getElementById('backgroundProposals');
  if (!listEl) return;
  listEl.innerHTML = '';
  if (_backgroundState.error && !_backgroundState.proposals.length) {
    var errorEl = document.createElement('div');
    errorEl.className = 'auth-note';
    errorEl.textContent = _backgroundState.error;
    listEl.appendChild(errorEl);
    return;
  }
  if (!_backgroundState.proposals.length) {
    var emptyEl = document.createElement('div');
    emptyEl.className = 'auth-note';
    emptyEl.textContent = 'No active proposals.';
    listEl.appendChild(emptyEl);
    return;
  }

  _backgroundState.proposals.forEach(function(proposal) {
    var proposalId = String(getBackgroundField(proposal, 'ProposalId', 'proposalId') || '');
    var status = String(getBackgroundField(proposal, 'Status', 'status') || 'pending');
    var jobType = String(getBackgroundField(proposal, 'JobType', 'jobType') || '');
    var createdAt = getBackgroundField(proposal, 'CreatedAt', 'createdAt');
    var notes = String(getBackgroundField(proposal, 'Notes', 'notes') || '');
    var windowStart = getBackgroundField(proposal, 'SourceWindowStart', 'sourceWindowStart');
    var windowEnd = getBackgroundField(proposal, 'SourceWindowEnd', 'sourceWindowEnd');
    var payload = getBackgroundPayload(proposal);
    var summary = String(payload.Summary || payload.summary || payload.Observation || payload.observation || 'No summary text.');

    var row = document.createElement('div');
    row.className = 'background-row';

    var head = document.createElement('div');
    head.className = 'background-row-head';
    var title = document.createElement('div');
    title.className = 'background-title';
    title.textContent = backgroundJobLabel(jobType);
    head.appendChild(title);

    var actions = document.createElement('div');
    actions.className = 'background-actions';
    if (status.toLowerCase() === 'pending') {
      var approveButton = document.createElement('button');
      approveButton.type = 'button';
      approveButton.className = 'auth-save background-inline-button';
      approveButton.textContent = 'Approve';
      approveButton.addEventListener('click', function() { reviewBackgroundProposal(proposalId, 'approve'); });
      actions.appendChild(approveButton);
      var rejectButton = document.createElement('button');
      rejectButton.type = 'button';
      rejectButton.className = 'auth-toggle';
      rejectButton.textContent = 'Reject';
      rejectButton.addEventListener('click', function() { reviewBackgroundProposal(proposalId, 'reject'); });
      actions.appendChild(rejectButton);
    } else if (status.toLowerCase() === 'applying') {
      var retryButton = document.createElement('button');
      retryButton.type = 'button';
      retryButton.className = 'auth-save background-inline-button';
      retryButton.textContent = 'Retry Apply';
      retryButton.addEventListener('click', function() { reviewBackgroundProposal(proposalId, 'approve'); });
      actions.appendChild(retryButton);
    }
    head.appendChild(actions);
    row.appendChild(head);

    var meta = document.createElement('div');
    meta.className = 'background-meta';
    ['Status: ' + status, 'Created: ' + formatGoalDate(createdAt), 'Proposal: ' + proposalId].forEach(function(text) {
      var item = document.createElement('span');
      item.textContent = text;
      meta.appendChild(item);
    });
    row.appendChild(meta);

    var source = document.createElement('div');
    source.className = 'background-meta';
    source.textContent = 'Source window: ' + formatGoalDate(windowStart) + ' to ' + formatGoalDate(windowEnd);
    row.appendChild(source);

    var summaryEl = document.createElement('div');
    summaryEl.className = 'background-description';
    summaryEl.textContent = summary;
    row.appendChild(summaryEl);
    if (notes) {
      var notesEl = document.createElement('div');
      notesEl.className = 'background-note';
      notesEl.textContent = notes;
      row.appendChild(notesEl);
    }
    listEl.appendChild(row);
  });
}

function renderBackgroundActivity() {
  var listEl = document.getElementById('backgroundActivity');
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!_backgroundState.activity.length) {
    var emptyEl = document.createElement('div');
    emptyEl.className = 'auth-note';
    emptyEl.textContent = 'No background activity yet.';
    listEl.appendChild(emptyEl);
    return;
  }
  _backgroundState.activity.forEach(function(activity) {
    var row = document.createElement('div');
    row.className = 'background-row background-activity-row';
    var status = String(getBackgroundField(activity, 'Status', 'status') || '');
    var jobType = String(getBackgroundField(activity, 'JobType', 'jobType') || '');
    var startedAt = getBackgroundField(activity, 'StartedAt', 'startedAt');
    var proposalCount = getBackgroundField(activity, 'ProposalCount', 'proposalCount');
    var notes = String(getBackgroundField(activity, 'Notes', 'notes') || '');
    var title = document.createElement('div');
    title.className = 'background-title';
    title.textContent = (jobType ? backgroundJobLabel(jobType) + ': ' : '') + (status || 'activity');
    row.appendChild(title);
    var meta = document.createElement('div');
    meta.className = 'background-meta';
    meta.textContent = formatGoalDate(startedAt) + ' | Proposals: ' + (proposalCount === '' ? 0 : proposalCount);
    row.appendChild(meta);
    if (notes) {
      var notesEl = document.createElement('div');
      notesEl.className = 'background-note';
      notesEl.textContent = notes;
      row.appendChild(notesEl);
    }
    listEl.appendChild(row);
  });
}

function renderBackgroundAll() {
  renderBackgroundStatus();
  renderBackgroundProposals();
  renderBackgroundActivity();
}

// ---------------------------------------------------------------------------
// Data retrieval mode switching (cloud vs local)
// ---------------------------------------------------------------------------
function switchDataMode(mode) {
  var bUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  bUrl = bUrl.replace(/\/+$/, '');
  var statusEl = document.getElementById('dataModeStatus');
  if (statusEl) statusEl.textContent = 'Switching to ' + mode + '...';

  fetch(bUrl + '/v1/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: mode }),
    signal: AbortSignal.timeout(30000)
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.error) {
      if (statusEl) statusEl.textContent = 'Error: ' + (d.error.message || d.error);
      return;
    }
    var sel = document.getElementById('selDataMode');
    if (sel) sel.value = d.mode;
    try { localStorage.setItem('evaDataMode', d.mode); } catch (_) {}
    if (statusEl) {
      if (d.mode === 'local') {
        statusEl.textContent = 'Local mode active (' + (d.local_tools || 0) + ' MCP tools available)';
      } else {
        statusEl.textContent = 'Cloud mode active (Copilot CLI)';
      }
    }
  })
  .catch(function(e) {
    if (statusEl) statusEl.textContent = 'Error: ' + e.message;
  });
}

function loadDataMode() {
  var bUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  bUrl = bUrl.replace(/\/+$/, '');
  fetch(bUrl + '/v1/mode', { signal: AbortSignal.timeout(3000) })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var sel = document.getElementById('selDataMode');
    if (sel) sel.value = d.mode || 'cloud';
    try { localStorage.setItem('evaDataMode', d.mode || 'cloud'); } catch (_) {}
    var statusEl = document.getElementById('dataModeStatus');
    if (statusEl) {
      var parts = [];
      if (d.cloud_available) parts.push('Cloud: available');
      else parts.push('Cloud: unavailable');
      if (d.local_available) parts.push('Local: ' + d.local_tools + ' tools');
      else parts.push('Local: not started');
      statusEl.textContent = parts.join(' | ');
    }
  })
  .catch(function() {});
}

// ---------------------------------------------------------------------------
// Doctor diagnostics
// ---------------------------------------------------------------------------
async function runDoctor() {
  var btn = document.getElementById('doctorButton');
  var report = document.getElementById('doctorReport');
  if (btn) btn.disabled = true;
  if (report) { report.style.display = 'block'; report.textContent = 'Running diagnostics...'; }
  try {
    var bridgeUrl = await detectACPBridge();
    var resp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/doctor');
    var data = await resp.json();
    if (!resp.ok) { if (report) report.textContent = 'Error: ' + (data.error ? data.error.message : 'unknown'); return; }

    // Format the report
    var lines = [];
    var r = data.readiness || {};
    var b = data.blockers || [];
    lines.push('=== Eva Diagnostics ===');
    lines.push('');
    lines.push('Readiness:');
    var checks = [
      ['Chat (ACP)', r.can_chat],
      ['Browser agent', r.can_browse],
      ['Desktop agent', r.can_desktop],
      ['Camera/vision', r.can_see],
      ['Memory (Kusto)', r.can_remember],
      ['Background loop', r.can_schedule],
      ['Cron tasks', r.can_cron],
    ];
    for (var i = 0; i < checks.length; i++) {
      lines.push('  ' + (checks[i][1] ? '\u2705' : '\u274c') + ' ' + checks[i][0]);
    }

    var ss = data.subsystems || {};
    if (ss.system) {
      lines.push('');
      lines.push('System: Python ' + (ss.system.python || '?') + ', Node ' + (ss.system.node || 'not found'));
      lines.push('Platform: ' + (ss.system.platform || '?') + ' (' + (ss.system.arch || '?') + ')');
    }
    if (ss.mcp) {
      lines.push('MCP servers: ' + (ss.mcp.configured || []).join(', ') || 'none');
    }
    if (ss.desktop_agent) {
    }
    if (b.length) {
      lines.push('');
      lines.push('Blockers:');
      for (var j = 0; j < b.length; j++) lines.push('  - ' + b[j]);
    }
    if (report) report.textContent = lines.join('\n');
  } catch (e) {
    if (report) report.textContent = 'Failed: ' + e.message + ' \u2014 Is the bridge running?';
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Cron scheduler UI
// ---------------------------------------------------------------------------
async function cronRefresh() {
  var listEl = document.getElementById('cronList');
  var statusEl = document.getElementById('cronStatus');
  try {
    var bridgeUrl = await detectACPBridge();
    var resp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron');
    var data = await resp.json();
    if (!resp.ok) { if (statusEl) statusEl.textContent = 'Error: ' + (data.error ? data.error.message : 'unknown'); return; }
    if (statusEl) statusEl.textContent = data.count + ' task(s)';
    if (!listEl) return;
    var tasks = data.tasks || [];
    if (!tasks.length) { listEl.innerHTML = '<p class="auth-note">No cron tasks. Add one above.</p>'; return; }
    var html = '';
    for (var i = 0; i < tasks.length; i++) {
      var t = tasks[i];
      var enabled = t.enabled !== false;
      html += '<div class="background-item" style="margin-bottom:10px;padding:8px;border:1px solid rgba(127,127,127,0.2);border-radius:6px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center">';
      html += '<strong>' + _escHtml(t.label) + '</strong>';
      html += '<span style="font-size:11px;opacity:0.7">' + _escHtml(t.schedule) + '</span>';
      html += '</div>';
      html += '<p style="margin:4px 0;font-size:12px;opacity:0.8">' + _escHtml(t.prompt).substring(0, 200) + '</p>';
      html += '<div style="font-size:11px;opacity:0.6">';
      if (t.next_run) html += 'Next: ' + t.next_run.substring(0, 16).replace('T', ' ') + ' UTC';
      if (t.last_run) html += ' | Last: ' + t.last_run.substring(0, 16).replace('T', ' ') + ' UTC';
      html += '</div>';
      html += '<div style="margin-top:6px;display:flex;gap:6px">';
      html += '<button class="auth-toggle" style="font-size:11px;padding:2px 8px" onclick="cronToggle(\'' + t.id + '\',' + !enabled + ')">' + (enabled ? 'Disable' : 'Enable') + '</button>';
      html += '<button class="auth-toggle" style="font-size:11px;padding:2px 8px;color:#c44" onclick="cronDelete(\'' + t.id + '\')">Delete</button>';
      html += '</div></div>';
    }
    listEl.innerHTML = html;
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Failed: ' + e.message;
  }
}

async function cronAdd() {
  var label = (document.getElementById('cronLabel') || {}).value || '';
  var schedule = (document.getElementById('cronSchedule') || {}).value || '';
  var prompt = (document.getElementById('cronPrompt') || {}).value || '';
  var statusEl = document.getElementById('cronStatus');
  if (!label.trim() || !schedule.trim() || !prompt.trim()) {
    if (statusEl) statusEl.textContent = 'All three fields are required.';
    return;
  }
  try {
    var bridgeUrl = await detectACPBridge();
    var resp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: label.trim(), schedule: schedule.trim(), prompt: prompt.trim() })
    });
    var data = await resp.json();
    if (!resp.ok) { if (statusEl) statusEl.textContent = 'Error: ' + (data.error ? data.error.message : 'unknown'); return; }
    if (statusEl) statusEl.textContent = 'Created: ' + (data.task ? data.task.label : '');
    // Clear form
    if (document.getElementById('cronLabel')) document.getElementById('cronLabel').value = '';
    if (document.getElementById('cronSchedule')) document.getElementById('cronSchedule').value = '';
    if (document.getElementById('cronPrompt')) document.getElementById('cronPrompt').value = '';
    cronRefresh();
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Failed: ' + e.message;
  }
}

async function cronToggle(taskId, enable) {
  try {
    var bridgeUrl = await detectACPBridge();
    await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron/' + encodeURIComponent(taskId), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: enable })
    });
    cronRefresh();
  } catch (e) { /* ignore */ }
}

async function cronDelete(taskId) {
  try {
    var bridgeUrl = await detectACPBridge();
    await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron/' + encodeURIComponent(taskId), { method: 'DELETE' });
    cronRefresh();
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Skills auto-learn — extract skill from recent interaction
// ---------------------------------------------------------------------------
async function autoLearnSkill(messages, taskSummary, capturedEnvelope) {
  function learningIsCurrent() {
    return !capturedEnvelope || _agentEnvelopeCurrent(capturedEnvelope);
  }
  if (!learningIsCurrent()) return null;
  try {
    var bridgeUrl = await detectACPBridge();
    if (!learningIsCurrent()) return null;
    var resp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/skills/auto-learn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: messages || [], task_summary: taskSummary || '' })
    });
    if (!learningIsCurrent()) return null;
    var data = await resp.json();
    if (!learningIsCurrent()) return null;
    if (resp.ok && data.draft) {
      // Auto-create the skill as a draft
      var draft = data.draft;
      if (!learningIsCurrent()) return null;
      var createResp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          Name: draft.Name || 'Auto-learned skill',
          Description: draft.Description || '',
          Instructions: draft.Instructions || '',
          Tools: draft.Tools || '',
          Tags: (draft.Tags || '') + (draft.Tags ? ',auto-learned' : 'auto-learned'),
          Source: 'auto-learned',
          Status: 'draft'
        })
      });
      if (!learningIsCurrent()) return null;
      if (createResp.ok) {
        setStatus('info', 'Skill learned: ' + (draft.Name || 'untitled'));
      }
      return data.draft;
    }
    return null;
  } catch (e) {
    return null;
  }
}

function _escHtml(s) {
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(s || ''));
  return div.innerHTML;
}

async function loadBackgroundData(quiet) {
  if (_backgroundState.loading) return;
  _backgroundState.loading = true;
  _backgroundState.error = '';
  if (!quiet) {
    _backgroundState.status = null;
    renderBackgroundStatus();
  }
  try {
    var options = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
      options.signal = AbortSignal.timeout(3000);
    }
    _backgroundState.status = await backgroundBridgeRequest('/v1/background/status', options);
    try {
      var proposalData = await backgroundBridgeRequest('/v1/background/proposals?status=all', options);
      var activityData = await backgroundBridgeRequest('/v1/background/activity', options);
      _backgroundState.proposals = Array.isArray(proposalData.proposals)
        ? proposalData.proposals.filter(function(p) {
            var s = String(getBackgroundField(p, 'Status', 'status') || '').toLowerCase();
            return s === 'pending' || s === 'applying';
          })
        : [];
      _backgroundState.activity = Array.isArray(activityData.activity) ? activityData.activity : [];
    } catch (listError) {
      _backgroundState.proposals = [];
      _backgroundState.activity = [];
      _backgroundState.error = listError.message || 'Background lists are not available right now.';
    }
  } catch (error) {
    _backgroundState.status = null;
    _backgroundState.proposals = [];
    _backgroundState.activity = [];
    _backgroundState.error = error.message || 'Background loop is not available right now.';
    if (!quiet) setStatus('warn', _backgroundState.error);
  } finally {
    _backgroundState.loading = false;
    renderBackgroundAll();
  }
}

async function saveBackgroundControls(runNow) {
  var enabledEl = document.getElementById('backgroundEnabled');
  var intervalEl = document.getElementById('backgroundIntervalSeconds');
  var saveButton = document.getElementById('backgroundSaveButton');
  var runButton = document.getElementById('backgroundRunNowButton');
  var intervalValue = intervalEl ? parseInt(intervalEl.value, 10) : 7200;
  if (!Number.isInteger(intervalValue) || intervalValue < 900 || intervalValue > 86400) {
    _backgroundState.error = 'Interval must be between 900 and 86400 seconds.';
    renderBackgroundStatus();
    setStatus('error', _backgroundState.error);
    return;
  }
  if (saveButton) saveButton.disabled = true;
  if (runButton) runButton.disabled = true;
  try {
    var jobs = {};
    var jobInputs = document.querySelectorAll('#backgroundJobs input[data-job]');
    Array.prototype.forEach.call(jobInputs, function(input) {
      jobs[input.getAttribute('data-job')] = !!input.checked;
    });
    var data = await backgroundBridgeRequest('/v1/background/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: enabledEl ? !!enabledEl.checked : true,
        intervalSeconds: intervalValue,
        jobs: jobs,
        runNow: !!runNow
      })
    });
    _backgroundState.status = data;
    _backgroundState.error = '';
    renderBackgroundStatus();
    await loadBackgroundData(true);
    setStatus('info', runNow ? 'Background run queued.' : 'Background controls saved.');
  } catch (error) {
    _backgroundState.error = error.message || 'Background control update failed.';
    renderBackgroundStatus();
    setStatus('error', _backgroundState.error);
  } finally {
    if (saveButton) saveButton.disabled = false;
    if (runButton) runButton.disabled = false;
  }
}

async function reviewBackgroundProposal(proposalId, action) {
  if (!proposalId) return;
  if (action === 'approve' && !confirm('Apply this proposal to Eva\u2019s memory?')) return;
  try {
    await backgroundBridgeRequest('/v1/background/proposals/' + encodeURIComponent(proposalId) + '/' + action, { method: 'POST' });
    await loadBackgroundData(true);
    setStatus('info', action === 'approve' ? 'Proposal applied.' : 'Proposal rejected.');
  } catch (error) {
    _backgroundState.error = error.message || 'Proposal review failed.';
    renderBackgroundAll();
    setStatus('error', _backgroundState.error);
  }
}

function initBackground() {
  var saveButton = document.getElementById('backgroundSaveButton');
  var runButton = document.getElementById('backgroundRunNowButton');
  var refreshButton = document.getElementById('backgroundRefreshButton');
  if (saveButton) saveButton.addEventListener('click', function() { saveBackgroundControls(false); });
  if (runButton) runButton.addEventListener('click', function() { saveBackgroundControls(true); });
  if (refreshButton) refreshButton.addEventListener('click', function() { loadBackgroundData(false); });
  renderBackgroundAll();
  loadBackgroundData(true);
}

// ---------------------------------------------------------------------------
// Proactive notifications — poll the bridge and surface findings in the chat
// ---------------------------------------------------------------------------
var _notifState = { polling: false, timer: null, intervalMs: 60000 };

function injectProactiveBubble(notif) {
  var txtOutput = document.getElementById('txtOutput');
  if (!txtOutput) return;
  if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
  var title = escapeHtml(String(notif.title || 'Eva'));
  var body = escapeHtml(String(notif.body || '')).replace(/\n/g, '<br>');
  var bubble =
    '<div class="chat-bubble eva-bubble eva-proactive">' +
    '<span class="eva">Eva:</span> ' +
    '<span class="eva-proactive-badge">Proactive</span> ' +
    '<strong>' + title + '</strong>' +
    '<div class="md">' + body + '</div></div>';
  txtOutput.innerHTML += bubble;
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

// ---------------------------------------------------------------------------
// Agent feedback loop — make Eva cognisant of what the browser/desktop agent
// actually did. Fired once when a run reaches a terminal state. It (1) renders
// a short Eva line summarizing the real outcome, (2) speaks it so the voice
// view stays in sync, and (3) appends an assistant-role note to the AIG
// conversation history so follow-up turns ("did it work?") are answered from
// fact rather than from the intent Eva announced before acting.
function _agentEnvelopeCurrent(capturedEnvelope) {
  return !!(
    capturedEnvelope &&
    typeof isCurrentRequestEnvelope === 'function' &&
    isCurrentRequestEnvelope(capturedEnvelope)
  );
}

function _isVerifiedAgentSuccess(status) {
  return typeof EvaActionOutcomes !== 'undefined' &&
    EvaActionOutcomes.isVerifiedSuccess(status);
}

function _evaAgentFeedback(status, endpoint, title, capturedEnvelope) {
  if (!_agentEnvelopeCurrent(capturedEnvelope) || !status) return;
  // Clear the progress-narration throttle so the completion line is never
  // suppressed as a near-duplicate of the last "working on it" update.
  try { if (typeof _agentProgress !== 'undefined') { _agentProgress.last = 0; _agentProgress.lastText = ''; } } catch (_) {}
  var label = (title || 'task').replace(/ Agent$/, '').toLowerCase();
  var goal = String(status.goal || '').trim();
  var outcome = (status.outcome && typeof status.outcome === 'object')
    ? status.outcome : null;
  var state = (typeof EvaActionOutcomes !== 'undefined')
    ? EvaActionOutcomes.displayState(status)
    : (outcome && outcome.state);
  if (state === 'indeterminate' && outcome && outcome.state === 'succeeded') {
    outcome = { state: state, reason: 'invalid_success_proof' };
  }
  if (!state) {
    // Legacy lifecycle status is not evidence. Preserve accurate failure/abort
    // wording, but never infer success from legacy `done`.
    if (status.status === 'error') state = 'failed';
    else if (status.status === 'cancelled') state = 'aborted';
    else if (status.status === 'done') state = 'indeterminate';
    else return;
    outcome = { state: state, reason: 'legacy_unverified_status' };
  }
  var spoken;     // natural, spoken/chat-facing sentence
  var memory;     // factual note for the conversation history

  if (state === 'succeeded') {
    var res = String(status.result || '').trim();
    var detail = res || 'The requested postcondition was verified.';
    spoken = /^(done|finished|all done|okay)/i.test(detail) ? detail : ('Verified. ' + detail);
    memory = 'Desktop/browser agent verified success' + (goal ? ' for "' + goal + '"' : '') + '. Result: ' + detail + '.';
  } else if (state === 'aborted') {
    var why = String(outcome.reason || 'aborted');
    if (why === 'budget_exhausted') {
      spoken = 'I reached the action limit and stopped without claiming success' + (goal ? ' on ' + goal : '') + '.';
    } else if (why === 'user_denied' || why === 'launch_not_authorized') {
      spoken = 'Okay, I held off' + (goal ? ' on ' + goal : '') + '.';
    } else {
      spoken = 'I stopped the ' + label + ' before completion was verified' + (goal ? ' for ' + goal : '') + '.';
    }
    memory = 'Desktop/browser agent aborted' + (goal ? ' for "' + goal + '"' : '') + '. Reason: ' + why + '.';
  } else if (state === 'failed') {
    var err = String(status.error || 'an unknown error').trim();
    spoken = 'I ran into a problem and could not finish' + (goal ? ' ' + goal : '') + ': ' + err + '.';
    memory = 'Desktop/browser agent failed' + (goal ? ' on "' + goal + '"' : '') + '. Error: ' + err + '.';
  } else if (state === 'indeterminate') {
    spoken = 'I stopped the ' + label + ', but I could not independently verify the result' + (goal ? ' for ' + goal : '') + '.';
    memory = 'Desktop/browser agent ended with an indeterminate outcome' + (goal ? ' for "' + goal + '"' : '') + '. No verified success was recorded.';
  } else {
    return;
  }

  // 1) Render an Eva chat bubble with the real outcome.
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(spoken).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }

  // 2) Speak it if auto-speak is on or the voice view is open, so the spoken
  //    narration reflects the actual result instead of the pre-action intent.
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vv.open);
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(spoken);
    }
  } catch (_) {}

  // 3) Append a factual assistant note to the AIG history so the next turn is
  //    grounded in what really happened.
  try {
    var storageKey = 'aigMessages';
    var hist = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(hist)) {
      hist.push({ role: 'assistant', content: '[Action outcome] ' + memory });
      localStorage.setItem(storageKey, JSON.stringify(hist));
    }
  } catch (_) {}

  if (typeof lastResponse === 'string') lastResponse = spoken;

  // 4) Return voice mode to listening after agent completion. The voice state
  //    machine may be stuck in 'speaking' or 'thinking' since the agent ran
  //    outside the normal turn cycle. Nudge it back after TTS finishes.
  try {
    if (typeof _vv !== 'undefined' && _vv.open) {
      setTimeout(function() {
        if (!_agentEnvelopeCurrent(capturedEnvelope)) return;
        if (_vv.phase === 'speaking' || _vv.phase === 'thinking') {
          if (typeof _vv._finishSpeaking === 'function') {
            _vv._finishSpeaking(false);
          } else {
            _vvStopTTS();
            _vvAfterTurn();
          }
        }
      }, 3000); // give TTS time to finish the completion sentence
    }
  } catch (_) {}

  // Learning remains an explicit, separately user-attested Phase 3 boundary.
  // Agent completion never invokes legacy auto-learning or Phase 3 reporting.
}

// Render + speak the result of an Eva "look" (webcam vision). Mirrors the agent
// feedback path: a chat bubble, optional speech, and a factual history note so
// follow-ups ("what colour was it?") are grounded in what she actually saw.
function _evaCameraLookResult(desc) {
  desc = String(desc || '').trim();
  if (!desc) return;
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(desc).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vv.open);
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(desc);
    }
  } catch (_) {}
  try {
    var hist = JSON.parse(localStorage.getItem('aigMessages') || '[]');
    if (Array.isArray(hist)) {
      hist.push({ role: 'assistant', content: '[Camera] I looked through the webcam and saw: ' + desc });
      localStorage.setItem('aigMessages', JSON.stringify(hist));
    }
  } catch (_) {}
  if (typeof lastResponse === 'string') lastResponse = desc;
}

// ---------------------------------------------------------------------------
// Natural agent confirmation — Eva asks in chat/voice instead of a popup button
// ---------------------------------------------------------------------------
// When the browser/desktop agent parks for the final purchase (or needs input),
// it calls _evaAgentConfirmAsk. Eva surfaces the question in chat (and speaks
// it), and _agentConfirm is armed so the user's next message is interpreted as
// the answer (yes/no, or free text) and routed to the agent rather than sent as
// a normal turn.
var _agentConfirm = {
  pending: false, needsText: false, gateId: null, expiresAt: null,
  envelope: null, _timeout: null
};

// Narrate agent progress so the user knows Eva is working and not stuck. Eva
// speaks/prints a short status when the plan changes, throttled so it does not
// chatter. Phrased as a brief present-tense update.
var _agentProgress = { last: 0, lastText: '' };
function _resetAgentInteractionState() {
  if (_agentConfirm._timeout) clearTimeout(_agentConfirm._timeout);
  _agentConfirm.pending = false;
  _agentConfirm.needsText = false;
  _agentConfirm.gateId = null;
  _agentConfirm.expiresAt = null;
  _agentConfirm.envelope = null;
  _agentConfirm._timeout = null;
  _agentProgress.last = 0;
  _agentProgress.lastText = '';
  try {
    var controller = (typeof EvaBrowser !== 'undefined' && EvaBrowser)
      ? EvaBrowser
      : ((typeof EvaDesktop !== 'undefined' && EvaDesktop) ? EvaDesktop : null);
    if (controller && typeof controller.cancel === 'function') controller.cancel();
  } catch (_) {}
}

function _evaAgentProgress(subgoal, status, capturedEnvelope) {
  if (!_agentEnvelopeCurrent(capturedEnvelope)) return;
  var sub = String(subgoal || '').trim();
  if (!sub) return;
  var now = Date.now();
  // Throttle: at most one spoken update every ~9s, and skip near-duplicates.
  if (now - _agentProgress.last < 9000) return;
  if (sub === _agentProgress.lastText) return;
  _agentProgress.last = now;
  _agentProgress.lastText = sub;
  var line = sub.charAt(0).toUpperCase() + sub.slice(1);
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(line).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble eva-proactive"><span class="eva">Eva:</span> <span class="eva-proactive-badge">working</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vv.open);
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(line);
    }
  } catch (_) {}
}

function _evaAgentConfirmAsk(question, needsText, status, capturedEnvelope) {
  if (!_agentEnvelopeCurrent(capturedEnvelope)) return;
  _agentConfirm.pending = true;
  _agentConfirm.needsText = !!needsText;
  _agentConfirm.gateId = status && status.approval_request && status.approval_request.gate_id;
  _agentConfirm.expiresAt = status && status.approval_request && status.approval_request.expires_at;
  _agentConfirm.envelope = capturedEnvelope;
  // Auto-cancel after 60s so a stale confirm doesn't block voice forever.
  if (_agentConfirm._timeout) clearTimeout(_agentConfirm._timeout);
  var expiryMs = Date.parse(_agentConfirm.expiresAt || '');
  var timeoutMs = Number.isFinite(expiryMs)
    ? Math.max(0, Math.min(60000, expiryMs - Date.now() - 250))
    : 0;
  _agentConfirm._timeout = setTimeout(function() {
    if (!_agentEnvelopeCurrent(capturedEnvelope)) {
      _resetAgentInteractionState();
      return;
    }
    if (_agentConfirm.pending) {
      console.warn('[AgentConfirm] Auto-cancelling after 60s timeout');
      _agentConfirm.pending = false;
      _agentConfirm.needsText = false;
      // Browser and desktop share one controller; send exactly one bound answer.
      try {
        if (typeof EvaBrowser !== 'undefined' && EvaBrowser &&
            typeof EvaBrowser.isAwaitingConfirm === 'function' && EvaBrowser.isAwaitingConfirm()) {
          EvaBrowser.answerConfirm(false, '', _agentConfirm.gateId);
        }
      } catch (_) {}
    }
  }, timeoutMs);
  var q = String(question || 'Should I continue?').trim();
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
    var safe = escapeHtml(q).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + safe + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vv.open);
    if ((voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(q);
    }
  } catch (_) {}
  if (typeof lastResponse === 'string') lastResponse = q;
}

// If an agent confirmation is pending, interpret `text` as the answer and route
// it to the agent. Returns true when the message was consumed (so the caller
// should NOT send it as a normal chat turn).
function _maybeAnswerAgentConfirm(text) {
  if (!_agentConfirm.pending) return false;
  if (!_agentEnvelopeCurrent(_agentConfirm.envelope)) {
    _resetAgentInteractionState();
    return false;
  }
  var confirmationEnvelope = _agentConfirm.envelope;
  var confirmationGateId = _agentConfirm.gateId;
  var active = (typeof EvaBrowser !== 'undefined' && EvaBrowser &&
                typeof EvaBrowser.isAwaitingConfirm === 'function' && EvaBrowser.isAwaitingConfirm());
  if (!active) { _agentConfirm.pending = false; return false; }
  var msg = String(text || '').trim();
  if (!msg) return false;

  // Free-text input request: pass the message straight through.
  if (_agentConfirm.needsText) {
    if (/^\s*(cancel|stop|abort|never\s*mind|nevermind)\s*[.!]?\s*$/i.test(msg)) {
      _agentConfirm.pending = false;
      _agentConfirm.needsText = false;
      EvaBrowser.answerConfirm(false, '', confirmationGateId);
      _agentConfirmEcho(msg, 'no', confirmationEnvelope);
      return true;
    }
    _agentConfirm.pending = false;
    _agentConfirm.needsText = false;
    EvaBrowser.answerConfirm(true, msg, confirmationGateId);
    _agentConfirmEcho(msg, null, confirmationEnvelope);
    return true;
  }

  var decision = (typeof EvaActionOutcomes !== 'undefined')
    ? EvaActionOutcomes.classifyApprovalReply(msg) : 'ambiguous';
  if (decision === 'ambiguous') {
    _agentConfirmEcho(msg, 'ambiguous', confirmationEnvelope);
    return true;
  }
  _agentConfirm.pending = false;
  var approved = decision === 'approve';
  EvaBrowser.answerConfirm(approved, '', confirmationGateId);
  _agentConfirmEcho(msg, approved ? 'yes' : 'no', confirmationEnvelope);
  return true;
}

function _agentConfirmEcho(userMsg, decision, capturedEnvelope) {
  if (!_agentEnvelopeCurrent(capturedEnvelope)) return;
  var txtOutput = document.getElementById('txtOutput');
  if (txtOutput) {
    var safeU = escapeHtml(String(userMsg)).replace(/\n/g, '<br>');
    txtOutput.innerHTML += '<div class="chat-bubble user-bubble"><span class="user">You:</span> ' + safeU + '</div>';
  }
  var reply = '';
  if (decision === 'yes') reply = 'Okay, confirming now.';
  else if (decision === 'no') reply = 'Understood, I\'ll stop and not place the order.';
  else if (decision === 'ambiguous') reply = 'Sorry, was that a yes or a no? Say yes to place the order or no to stop.';
  if (reply && txtOutput) {
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + escapeHtml(reply) + '</div></div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
  }
  try {
    var autoSpeakEl = document.getElementById('autoSpeak');
    var voiceOpen = (typeof _vv !== 'undefined' && _vv.open);
    if (reply && (voiceOpen || (autoSpeakEl && autoSpeakEl.checked)) && typeof speakText === 'function') {
      speakText(reply);
    }
  } catch (_) {}
}

async function pollNotifications() {
  if (_notifState.polling) return;
  _notifState.polling = true;
  try {
    var options = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
      options.signal = AbortSignal.timeout(4000);
    }
    var data = await backgroundBridgeRequest('/v1/notifications?unseen_only=1&limit=10', options);
    var items = (data && Array.isArray(data.notifications)) ? data.notifications : [];
    if (!items.length) return;
    var seenIds = [];
    var voiceText = [];
    items.forEach(function(notif) {
      injectProactiveBubble(notif);
      var channels = Array.isArray(notif.channels) ? notif.channels : ['chat'];
      if (channels.indexOf('voice') !== -1 && notif.body) {
        voiceText.push(String(notif.title || '') + '. ' + String(notif.body || ''));
      }
      if (notif.id) seenIds.push(notif.id);
    });
    // Speak queued voice notifications one combined utterance to avoid overlap.
    if (voiceText.length && typeof speakText === 'function') {
      try { speakText(voiceText.join('. ')); } catch (_) {}
    }
    if (seenIds.length) {
      try {
        await backgroundBridgeRequest('/v1/notifications/seen', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: seenIds })
        });
      } catch (_) {}
    }
  } catch (_) {
    // Bridge unreachable or notifications unavailable; stay quiet and retry next tick.
  } finally {
    _notifState.polling = false;
  }
}

function initNotifications() {
  if (_notifState.timer) return;
  // First poll shortly after load, then on a steady cadence.
  setTimeout(pollNotifications, 8000);
  _notifState.timer = setInterval(pollNotifications, _notifState.intervalMs);
}

// ---------------------------------------------------------------------------
// Alerts — user-defined watches the background loop evaluates each cycle
// ---------------------------------------------------------------------------
var _alertsState = { alerts: [], settings: {} };

function alertTypeLabel(type) {
  switch (String(type || '')) {
    case 'keyword_watch': return 'Topic watch';
    case 'research_question': return 'Research question';
    case 'sec_filing': return 'SEC filings';
    case 'weather': return 'Weather';
    case 'space_weather': return 'Space weather';
    default: return type || 'Alert';
  }
}

// The single primary input is relabeled per type; weather adds a condition field.
function updateAlertParamFields() {
  var type = (document.getElementById('alertType') || {}).value || 'keyword_watch';
  var topicWrap = document.getElementById('alertParamTopicWrap');
  var topicLabel = document.getElementById('alertParamTopicLabel');
  var topicInput = document.getElementById('alertParamTopic');
  var condWrap = document.getElementById('alertParamConditionWrap');
  var showTopic = true, showCond = false, label = 'Topic to watch', ph = '';
  switch (type) {
    case 'keyword_watch': label = 'Topic to watch'; ph = 'e.g. new OpenAI model releases'; break;
    case 'research_question': label = 'Question to track'; ph = 'e.g. has the Fed changed interest rates?'; break;
    case 'sec_filing': label = 'Ticker symbols (comma separated)'; ph = 'e.g. AAPL, MSFT'; break;
    case 'weather': label = 'Location'; ph = 'e.g. Seattle, WA'; showCond = true; break;
    case 'space_weather': showTopic = false; break;
  }
  if (topicWrap) topicWrap.style.display = showTopic ? '' : 'none';
  if (topicLabel) topicLabel.textContent = label;
  if (topicInput) topicInput.placeholder = ph;
  if (condWrap) condWrap.style.display = showCond ? '' : 'none';
}

function buildAlertParams(type, topicVal, condVal) {
  switch (type) {
    case 'keyword_watch': return { topic: topicVal };
    case 'research_question': return { question: topicVal };
    case 'sec_filing': return { symbols: topicVal };
    case 'weather': return { location: topicVal, condition: condVal };
    case 'space_weather': return {};
    default: return {};
  }
}

function renderAlertsList() {
  var listEl = document.getElementById('alertsList');
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!_alertsState.alerts.length) {
    var empty = document.createElement('div');
    empty.className = 'auth-note';
    empty.textContent = 'No alerts yet. Add one above to have Eva watch for you.';
    listEl.appendChild(empty);
    return;
  }
  _alertsState.alerts.forEach(function(rule) {
    var row = document.createElement('div');
    row.className = 'background-row';
    var head = document.createElement('div');
    head.className = 'background-row-head';
    var title = document.createElement('div');
    title.className = 'background-title';
    title.textContent = rule.label + (rule.enabled ? '' : ' (paused)');
    head.appendChild(title);
    var actions = document.createElement('div');
    actions.className = 'background-actions';
    var toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'auth-toggle background-inline-button';
    toggleBtn.textContent = rule.enabled ? 'Pause' : 'Resume';
    toggleBtn.addEventListener('click', function() { toggleAlert(rule.id); });
    actions.appendChild(toggleBtn);
    var delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'auth-toggle';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', function() { deleteAlert(rule.id); });
    actions.appendChild(delBtn);
    head.appendChild(actions);
    row.appendChild(head);
    var meta = document.createElement('div');
    meta.className = 'background-meta';
    var p = rule.params || {};
    var detail = p.topic || p.question || p.location || (p.symbols ? p.symbols.join(', ') : '') || alertTypeLabel(rule.type);
    ['Type: ' + alertTypeLabel(rule.type), detail, 'Every ' + Math.round((rule.cooldown_min || 1440) / 60) + 'h',
     'Via: ' + (rule.channels || []).join(', ')].forEach(function(text) {
      if (!text) return;
      var span = document.createElement('span');
      span.textContent = text;
      meta.appendChild(span);
    });
    row.appendChild(meta);
    if (rule.last_fired_iso) {
      var last = document.createElement('div');
      last.className = 'background-note';
      last.textContent = 'Last fired: ' + formatGoalDate(rule.last_fired_iso);
      row.appendChild(last);
    }
    listEl.appendChild(row);
  });
}

async function loadAlerts() {
  try {
    var data = await backgroundBridgeRequest('/v1/alerts', { method: 'GET' });
    _alertsState.alerts = Array.isArray(data.alerts) ? data.alerts : [];
    _alertsState.settings = data.settings || {};
    renderAlertsList();
    var s = _alertsState.settings;
    var qs = document.getElementById('alertQuietStart');
    var qe = document.getElementById('alertQuietEnd');
    var mph = document.getElementById('alertMaxPerHour');
    if (qs && typeof s.quiet_hours_start === 'number') qs.value = s.quiet_hours_start;
    if (qe && typeof s.quiet_hours_end === 'number') qe.value = s.quiet_hours_end;
    if (mph && typeof s.max_per_hour === 'number') mph.value = s.max_per_hour;
  } catch (error) {
    var listEl = document.getElementById('alertsList');
    if (listEl) listEl.innerHTML = '<div class="auth-note">' + escapeHtml(error.message || 'Alerts unavailable.') + '</div>';
  }
}

function readAlertForm() {
  var type = (document.getElementById('alertType') || {}).value || 'keyword_watch';
  var label = (document.getElementById('alertLabel') || {}).value || '';
  var topic = (document.getElementById('alertParamTopic') || {}).value || '';
  var cond = (document.getElementById('alertParamCondition') || {}).value || '';
  var cooldownHours = parseInt((document.getElementById('alertCooldown') || {}).value, 10);
  if (!Number.isInteger(cooldownHours) || cooldownHours < 1) cooldownHours = 24;
  var channels = [];
  if ((document.getElementById('alertChannelChat') || {}).checked) channels.push('chat');
  if ((document.getElementById('alertChannelVoice') || {}).checked) channels.push('voice');
  if (!channels.length) channels.push('chat');
  return {
    type: type,
    label: label.trim() || alertTypeLabel(type),
    params: buildAlertParams(type, topic.trim(), cond.trim()),
    cooldown_min: cooldownHours * 60,
    channels: channels,
    enabled: (document.getElementById('alertEnabled') || {}).checked !== false
  };
}

async function saveAlert() {
  var rule = readAlertForm();
  // Client-side validation for required fields
  var type = rule.type;
  var params = rule.params || {};
  if (type === 'keyword_watch' && !params.topic) {
    setStatus('error', 'Topic to watch is required.');
    var topicInput = document.getElementById('alertParamTopic');
    if (topicInput) topicInput.focus();
    return;
  }
  if (type === 'research_question' && !params.question) {
    setStatus('error', 'Question to track is required.');
    var topicInput = document.getElementById('alertParamTopic');
    if (topicInput) topicInput.focus();
    return;
  }
  if (type === 'sec_filing' && !params.symbols) {
    setStatus('error', 'At least one ticker symbol is required.');
    var topicInput = document.getElementById('alertParamTopic');
    if (topicInput) topicInput.focus();
    return;
  }
  if (type === 'weather' && !params.location) {
    setStatus('error', 'Location is required.');
    var topicInput = document.getElementById('alertParamTopic');
    if (topicInput) topicInput.focus();
    return;
  }
  try {
    await backgroundBridgeRequest('/v1/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rule)
    });
    clearAlertForm();
    await loadAlerts();
    setStatus('info', 'Alert saved.');
  } catch (error) {
    setStatus('error', error.message || 'Could not save alert.');
  }
}

async function toggleAlert(id) {
  var rule = _alertsState.alerts.filter(function(r) { return r.id === id; })[0];
  if (!rule) return;
  rule.enabled = !rule.enabled;
  try {
    await backgroundBridgeRequest('/v1/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rule)
    });
    await loadAlerts();
  } catch (error) {
    setStatus('error', error.message || 'Could not update alert.');
  }
}

async function deleteAlert(id) {
  if (!confirm('Delete this alert?')) return;
  try {
    await backgroundBridgeRequest('/v1/alerts/' + encodeURIComponent(id), { method: 'DELETE' });
    await loadAlerts();
    setStatus('info', 'Alert deleted.');
  } catch (error) {
    setStatus('error', error.message || 'Could not delete alert.');
  }
}

async function saveAlertSettings() {
  var payload = {
    quiet_hours_start: parseInt((document.getElementById('alertQuietStart') || {}).value, 10),
    quiet_hours_end: parseInt((document.getElementById('alertQuietEnd') || {}).value, 10),
    max_per_hour: parseInt((document.getElementById('alertMaxPerHour') || {}).value, 10)
  };
  try {
    await backgroundBridgeRequest('/v1/alerts/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    await loadAlerts();
    setStatus('info', 'Notification limits saved.');
  } catch (error) {
    setStatus('error', error.message || 'Could not save limits.');
  }
}

function clearAlertForm() {
  var ids = ['alertLabel', 'alertParamTopic', 'alertParamCondition'];
  ids.forEach(function(id) { var el = document.getElementById(id); if (el) el.value = ''; });
  var cd = document.getElementById('alertCooldown'); if (cd) cd.value = 24;
  var en = document.getElementById('alertEnabled'); if (en) en.checked = true;
}

function initAlerts() {
  var typeSel = document.getElementById('alertType');
  var saveBtn = document.getElementById('alertSaveButton');
  var clearBtn = document.getElementById('alertClearButton');
  var settingsBtn = document.getElementById('alertSettingsSaveButton');
  if (typeSel) typeSel.addEventListener('change', updateAlertParamFields);
  if (saveBtn) saveBtn.addEventListener('click', saveAlert);
  if (clearBtn) clearBtn.addEventListener('click', clearAlertForm);
  if (settingsBtn) settingsBtn.addEventListener('click', saveAlertSettings);
  updateAlertParamFields();
  loadAlerts();
}

function applyStandaloneSimplifications() {
  if (!(typeof isEvaStandalone === 'function' && isEvaStandalone())) return;

  var selModel = document.getElementById('selModel');
  if (selModel) {
    var modelChanged = selModel.value !== 'aig';
    Array.from(selModel.children).forEach(function(child) {
      if (child.tagName === 'OPTGROUP') {
        var hasAigOption = false;
        Array.from(child.children).forEach(function(option) {
          if (option.value === 'aig') {
            hasAigOption = true;
          } else {
            option.remove();
          }
        });
        if (!hasAigOption) child.remove();
      } else if (child.tagName === 'OPTION' && child.value !== 'aig') {
        child.remove();
      }
    });

    selModel.value = 'aig';
    var modelLabel = document.querySelector('label[for="selModel"]');
    if (modelLabel) modelLabel.style.display = 'none';
    selModel.style.display = 'none';

    if (modelChanged) {
      selModel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  var engineSelect = document.getElementById('selEngine');
  var barkOption = document.querySelector('#selEngine option[value="bark"]');
  if (engineSelect) {
    var current = engineSelect.value;
    var pollyEngine = (current === 'standard' || current === 'neural' || current === 'generative');
    if (!current || current === 'bark' || pollyEngine) {
      var hasOpenAIKey = (typeof getAuthKey === 'function') ? !!getAuthKey('OPENAI_API_KEY') : !!window.OPENAI_API_KEY;
      engineSelect.value = hasOpenAIKey ? 'openai' : 'browser';
    }
  }
  var pollyValues = ['standard', 'neural', 'generative'];
  pollyValues.forEach(function (val) {
    var opt = document.querySelector('#selEngine option[value="' + val + '"]');
    if (opt) opt.remove();
  });
  if (barkOption) barkOption.remove();

  var standaloneVersionEl = document.getElementById('evaStandaloneVersion');
  if (standaloneVersionEl && window.evaStandalone && window.evaStandalone.version) {
    standaloneVersionEl.textContent = 'Standalone v' + window.evaStandalone.version;
    standaloneVersionEl.style.display = '';
  }
}

function applyStandaloneSurface() {
  applyStandaloneSimplifications();
}

function getSavedMCPConfig() {
  try {
    return JSON.parse(localStorage.getItem('mcp_config') || '{}') || {};
  } catch (error) {
    return {};
  }
}

function hasSavedStandaloneKustoConfig() {
  var config = getSavedMCPConfig();
  var kusto = config['kusto-mcp-server'];
  var env = kusto && kusto.env ? kusto.env : {};
  return !!(env.KUSTO_CLUSTER_URL && String(env.KUSTO_CLUSTER_URL).trim());
}

function initStandaloneFirstRun() {
  if (!(typeof isEvaStandalone === 'function' && isEvaStandalone())) return;
  // If no memory backend has been chosen yet, default to SQLite and seed
  if (!localStorage.getItem('eva_memory_backend') && !hasSavedStandaloneKustoConfig()) {
    localStorage.setItem('eva_memory_backend', 'sqlite');
    localStorage.setItem('eva_standalone_first_run_done', '1');
    var bridgeUrl = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : '';
    if (bridgeUrl) {
      fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/backend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: 'sqlite' }),
        signal: AbortSignal.timeout(5000)
      }).catch(function() {});
    }
    var memSel = document.getElementById('memoryBackendSelect');
    if (memSel) memSel.value = 'sqlite';
  }
}

function toggleAuthVis(btn) {
  var input = btn.parentElement.querySelector('input');
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = 'Hide';
  } else {
    input.type = 'password';
    btn.textContent = 'Show';
  }
}

// --- System Prompt Management ---
var PERSONALITY_PRESETS = {
  'default': "You are Eva, a warm, concise personal AI assistant with persistent memory. Use [Memory], [User Profile], and [Data Retrieved] honestly. For one bounded public-browser task, emit one mandatory closed [[EVA_BROWSER]] marker and include a deterministic request postcondition when known. For desktop control, only an allowlisted GUI launch is available: use one mandatory closed [[EVA_DESKTOP]] marker with a desktop.process_spawned started postcondition. Electron authorizes the complete launch, and every browser effect or desktop launch requires a separate approval. Model done and step limits are not success; only a typed causal tool-verified postcondition outcome is success. Desktop pointer, keyboard, shell, arguments, window focus, and arbitrary file-open control are unavailable. Never emit browser and desktop markers together. Use [[EVA_LOOK]] for webcam vision, [[EVA_SIGNAL]] for requested Signal text, [Image of <description>] for images, and [[EVA_FILE]] <filename.ext> for a file that was actually created. Never fabricate live data or claim an action completed without the verified outcome. When asked your model, answer only from [Runtime].",
  'concise': "You are Eva. Be direct and brief. Use memory and retrieved data accurately. Browser runs require one closed marker, native launch authorization, per-effect approval, and a deterministic postcondition for verified success. Desktop is allowlisted GUI launch-only with a process-start postcondition; pointer, keyboard, shell, arguments, window focus, and arbitrary file-open control are unavailable. Never treat model done or a step limit as success, and never emit multiple control markers.",
  'advanced': "You are Eva, a detailed personal AI assistant with persistent memory and real-time data access. Use only registered capabilities. A browser marker requests one isolated public-browser run; include a deterministic URL or bounded element-state postcondition when known. A desktop marker requests only an allowlisted GUI launch with a desktop.process_spawned started postcondition. Electron displays and authorizes the exact launch specification, and every effect requires a separate one-use approval. Only a typed causal baseline-to-effect-to-tool-observation outcome is success. Model done, step limits, and unverified claims are not success. Desktop pointer, keyboard, shell, arguments, window focus, and arbitrary file-open control are unavailable. Never emit browser and desktop markers together. Never fabricate live data or report an action before its verified outcome.",
  'terminal': "I want you to act as a linux terminal. I will type commands and you will reply with what the terminal should show. I want you to only reply with the terminal output inside one unique code block, and nothing else. do not write explanations. do not type commands unless I instruct you to do so. when i need to tell you something in english, i will do so by putting text inside curly brackets {like this}. my first command is pwd"
};

function getSystemPrompt() {
  var txt = document.getElementById('txtSystemPrompt');
  if (txt && txt.value.trim()) return txt.value.trim();
  // Fallback to personality preset
  return PERSONALITY_PRESETS['default'];
}

function applyPersonalityPreset() {
  var sel = document.getElementById('selPers');
  var txt = document.getElementById('txtSystemPrompt');
  if (!sel || !txt) return;
  var preset = PERSONALITY_PRESETS[sel.value];
  if (preset) {
    txt.value = preset;
    localStorage.setItem('systemPrompt', preset);
  }
  // 'custom' leaves textarea as-is for user editing
}

// Old presets that should be auto-migrated to current versions.
// If a user's saved prompt matches one of these stale strings, replace it
// with the corresponding current preset so new capabilities (camera, etc.)
// are picked up without manual intervention.
var _STALE_PRESETS = {
  'default': "You are Eva, an AI assistant with persistent memory and real-time data access. You can look up live stock prices, weather, news, space weather, and market data; search the web; generate and find images; query your memory and knowledge store; SEE through the user's webcam by emitting [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]] to capture a frame and describe it; and send Signal messages by emitting [[EVA_SIGNAL]]{\"message\":\"<text>\"}[[/EVA_SIGNAL]] when the user asks to text them. For actionable requests, use the tools and agents available to you rather than just describing steps. When a user asks for a downloadable artifact, emit [[EVA_ACTION]]{\"id\":\"<capability-id>\",\"args\":{...}}[[/EVA_ACTION]] on its own line. For browser-based tasks, prefer the built-in browser agent; for desktop or app tasks, use the desktop agent when available. Only claim an action happened after it actually ran. Always try to fulfill requests using your available tools and data before saying you cannot. Be accurate, helpful, and straightforward.",
  'concise': "You are Eva. Capabilities: persistent memory, real-time data (stocks, weather, news, markets), web search, image generation, Kusto database queries, webcam vision (emit [[EVA_LOOK]]{\"question\":\"...\"}[[/EVA_LOOK]] to capture and describe a frame). Answer factual questions concisely. Use your tools to fetch live data when asked.",
  'advanced': "You are Eva, an intelligent AI assistant with full tool access. You can: retrieve live stock quotes and financial data, fetch weather/news/market/space weather feeds, search the web and retrieve information, generate and find images, query your Kusto persistent memory database (tables: Knowledge, Conversations, EmotionState, MemorySummaries, SelfState, HeuristicsIndex, Reflections, EmotionBaseline), and SEE through the user's webcam by emitting [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]] to capture a frame and describe it. Do NOT claim you cannot access the camera. You remember the user across sessions. Provide detailed, well-structured responses with lists where applicable. Always attempt to use your tools before claiming inability."
};
var _STALE_ACTION_PROMPT_RE = /Browser agent:\s*emit|Desktop control:\s*emit|full agent capabilities|Act immediately\s*[—-]\s*emit the marker/i;

function initSystemPrompt() {
  var txt = document.getElementById('txtSystemPrompt');
  if (!txt) return;
  // Load from localStorage or use default preset
  var saved = localStorage.getItem('systemPrompt');
  if (saved) {
    // Auto-migrate stale presets to current versions
    var trimmed = saved.trim();
    var migrated = false;
    Object.keys(_STALE_PRESETS).forEach(function(k) {
      if (!migrated && trimmed === _STALE_PRESETS[k]) {
        saved = PERSONALITY_PRESETS[k];
        localStorage.setItem('systemPrompt', saved);
        migrated = true;
      }
    });
    if (!migrated && /^You are Eva\b/i.test(trimmed) && _STALE_ACTION_PROMPT_RE.test(trimmed)) {
      saved = PERSONALITY_PRESETS['default'];
      localStorage.setItem('systemPrompt', saved);
    }
    txt.value = saved;
    // Sync preset selector
    var sel = document.getElementById('selPers');
    if (sel) {
      var matched = false;
      Object.keys(PERSONALITY_PRESETS).forEach(function(k) {
        if (PERSONALITY_PRESETS[k] === saved.trim()) { sel.value = k; matched = true; }
      });
      if (!matched) sel.value = 'custom';
    }
  } else {
    txt.value = PERSONALITY_PRESETS['default'];
  }
  // Save on change
  txt.addEventListener('input', function() {
    localStorage.setItem('systemPrompt', txt.value);
    var sel = document.getElementById('selPers');
    if (sel) {
      var matched = false;
      Object.keys(PERSONALITY_PRESETS).forEach(function(k) {
        if (PERSONALITY_PRESETS[k] === txt.value.trim()) { sel.value = k; matched = true; }
      });
      if (!matched) sel.value = 'custom';
    }
  });
}

// --- Model Settings Helpers ---
function getModelTemperature() {
  var el = document.getElementById('sldTemperature');
  return el ? parseFloat(el.value) : 0.7;
}

function getModelMaxTokens() {
  var el = document.getElementById('txtMaxTokens');
  return el ? (parseInt(el.value) || 4096) : 4096;
}

function getReasoningEffort() {
  var el = document.getElementById('selReasoningEffort');
  return el ? el.value : 'medium';
}

var _modeInitDone = false;
function onModelSettingsChange() {
  var sel = document.getElementById('selModel');
  if (!sel) return;
  var model = sel.value;
  // Show/hide reasoning effort (for reasoning models)
  var reOpt = document.getElementById('opt-reasoningEffort');
  if (reOpt) {
    var reasoningModels = ['o3-mini', 'copilot-o3-mini', 'copilot-o4-mini', 'copilot-deepseek-r1'];
    reOpt.style.display = reasoningModels.indexOf(model) >= 0 ? 'block' : 'none';
  }
  // Show/hide temperature (hidden for reasoning models, gpt-5 family, latest, copilot-acp)
  var tempOpt = document.getElementById('opt-temperature');
  if (tempOpt) {
    var hideTemp = ['o3-mini', 'copilot-o3-mini', 'copilot-o4-mini', 'copilot-deepseek-r1', 'copilot-gpt-5', 'gpt-5-mini', 'latest', 'copilot-acp', 'aig'].indexOf(model) >= 0;
    tempOpt.style.display = hideTemp ? 'none' : 'block';
  }
  // Show/hide AIG backend model selector (only for aig)
  var aigOpt = document.getElementById('opt-aigBackend');
  if (aigOpt) {
    aigOpt.style.display = (model === 'aig') ? 'block' : 'none';
  }
  // Show/hide ACP model selector (only for copilot-acp)
  var acpOpt = document.getElementById('opt-acpModel');
  if (acpOpt) {
    acpOpt.style.display = (model === 'copilot-acp') ? 'block' : 'none';
  }

  // Auto-switch data retrieval mode based on selected model.
  // lm-studio (or aig with lmstudio backend) -> local mode
  // Cloud models: leave the user's persisted mode choice alone.
  // Skip during init — the bridge (mode.txt) is the source of truth at startup.
  if (_modeInitDone) {
    var needsLocal = (model === 'lm-studio') ||
      (model === 'aig' && (localStorage.getItem('aigBackend') || '') === 'lmstudio');
    var currentMode = (document.getElementById('selDataMode') || {}).value || 'cloud';
    if (needsLocal && currentMode !== 'local') {
      switchDataMode('local');
    }
  }
}

function getACPModel() {
  var el = document.getElementById('selACPModel');
  return (el && el.value) ? el.value : '';
}

// --- Model option filtering per theme (LCARS-restricted) ---
// Cache of the original full model list and last non-LCARS selection
var __originalModelOptions = null;
var __modelBeforeLCARS = null;

function captureOriginalModelOptions() {
  if (__originalModelOptions) return;
  var sel = document.getElementById('selModel');
  if (!sel) return;
  __originalModelOptions = [];
  // Walk through child nodes to preserve optgroup structure
  Array.from(sel.children).forEach(function(child) {
    if (child.tagName === 'OPTGROUP') {
      var group = { label: child.label, options: [] };
      Array.from(child.children).forEach(function(o) {
        group.options.push({ value: o.value, text: o.text, title: o.title || '' });
      });
      __originalModelOptions.push({ type: 'optgroup', group: group });
    } else if (child.tagName === 'OPTION') {
      __originalModelOptions.push({ type: 'option', value: child.value, text: child.text, title: child.title || '' });
    }
  });
}

function setModelOptions(list) {
  var sel = document.getElementById('selModel');
  if (!sel) return;
  var currentValue = sel.value;
  sel.innerHTML = '';
  list.forEach(function(item) {
    if (item.type === 'optgroup') {
      var grp = document.createElement('optgroup');
      grp.label = item.group.label;
      item.group.options.forEach(function(o) {
        var opt = document.createElement('option');
        opt.value = o.value;
        opt.text = o.text;
        if (o.title) opt.title = o.title;
        grp.appendChild(opt);
      });
      sel.appendChild(grp);
    } else {
      var opt = document.createElement('option');
      opt.value = item.value;
      opt.text = item.text;
      if (item.title) opt.title = item.title;
      sel.appendChild(opt);
    }
  });
  // Try to keep current selection if still present; otherwise select first
  var allOpts = Array.from(sel.options);
  var hasCurrent = allOpts.some(function(o) { return o.value === currentValue; });
  sel.value = hasCurrent ? currentValue : (allOpts[0] ? allOpts[0].value : '');
  // Trigger change to rewire send behavior
  sel.dispatchEvent(new Event('change', { bubbles: true }));
}

function updateModelOptionsForTheme(theme) {
  captureOriginalModelOptions();
  var sel = document.getElementById('selModel');
  if (!sel) return;
  if (theme === 'lcars') {
    // Remember current model to restore when leaving LCARS
    __modelBeforeLCARS = sel.value;
    var allowed = new Set(['gpt-5-mini', 'o3-mini', 'dall-e-3', 'gemini', 'lm-studio', 'copilot-gpt-4o', 'copilot-gpt-4o-mini', 'copilot-o3-mini', 'copilot-gpt-4.1', 'copilot-gpt-5', 'copilot-o4-mini', 'copilot-deepseek-r1', 'copilot-llama-4-maverick', 'copilot-acp', 'aig']);
    var filtered = [];
    (__originalModelOptions || []).forEach(function(item) {
      if (item.type === 'optgroup') {
        var filteredOpts = item.group.options.filter(function(o) { return allowed.has(o.value); });
        if (filteredOpts.length) {
          filtered.push({ type: 'optgroup', group: { label: item.group.label, options: filteredOpts } });
        }
      } else if (allowed.has(item.value)) {
        filtered.push(item);
      }
    });
    if (filtered.length) setModelOptions(filtered);
  } else if (__originalModelOptions) {
    setModelOptions(__originalModelOptions);
    // Restore pre-LCARS selection if it exists
    if (__modelBeforeLCARS) {
      var hasPrev = Array.from(sel.options).some(function(o){ return o.value === __modelBeforeLCARS; });
      if (hasPrev) {
        sel.value = __modelBeforeLCARS;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  }
  applyStandaloneSimplifications();
}

// Settings Menu Options 
document.addEventListener('DOMContentLoaded', () => {
  const settingsButton = document.getElementById('settingsButton');
  const settingsMenu = document.getElementById('settingsMenu');
  const themeSelect = document.getElementById('selTheme');
  const lcarsChipSand = document.getElementById('lcarsChipSand');
  const speakBtn = document.getElementById('speakSend');
  const selModel = document.getElementById('selModel');
  // LCARS sidebar controls (optional)
  const sidebarSettingsBtn = document.getElementById('sidebarSettingsBtn');
  const sidebarClearBtn = document.getElementById('sidebarClearBtn');
  const lcarsLabel = document.querySelector('#lcarsSidebar .lcars-label');
  const lcarsChipPrint = document.getElementById('lcarsChipPrint');
  const printBtn = document.getElementById('printButton');
  const lcarsChipTop = document.getElementById('lcarsChipTop');
  const monitorTabs = document.getElementById('lcarsMonitorTabs');
  const monitorPanels = document.getElementById('lcarsMonitorPanels');

  applyStandaloneSurface();
  initStandaloneFirstRun();

  // Persist and restore the AIG backend selection across restarts.
  var aigBackendSel = document.getElementById('selAIGBackend');
  if (aigBackendSel) {
    var savedAigBackend = localStorage.getItem('aigBackend');
    if (savedAigBackend) {
      var hasOpt = Array.from(aigBackendSel.options).some(function (o) { return o.value === savedAigBackend; });
      if (hasOpt) aigBackendSel.value = savedAigBackend;
    }
    aigBackendSel.addEventListener('change', function () {
      localStorage.setItem('aigBackend', aigBackendSel.value);
      // Keep cognition model selectors in sync with the live catalog.
      if (typeof cogInit === 'function') cogInit();
      // Re-evaluate data mode when AIG backend changes
      if (typeof onModelSettingsChange === 'function') onModelSettingsChange();
    });

    // Auto-detect LM Studio: if the user has never explicitly picked a backend,
    // probe the LM Studio endpoint and switch to it when reachable.
    if (!savedAigBackend) {
      var lmsUrl = (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : 'http://localhost:1234/v1';
      fetch(lmsUrl + '/models', { signal: AbortSignal.timeout(2000) })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (data) {
          if (data && data.data && data.data.length > 0) {
            aigBackendSel.value = 'lmstudio';
            localStorage.setItem('aigBackend', 'lmstudio');
            aigBackendSel.dispatchEvent(new Event('change', { bubbles: true }));
            console.log('[Eva] LM Studio detected, set as default backend');
          }
        })
        .catch(function () { /* LM Studio not running, keep current default */ });
    }
  }

  // Camera presence (auto-wake): toggle the local webcam sensor. Restore the
  // persisted choice, but only auto-start the camera if it was previously on.
  var cameraPresenceEl = document.getElementById('cameraPresence');
  if (cameraPresenceEl) {
    var savedCam = false;
    var hadLocal = false;
    try {
      var lv = localStorage.getItem('cameraPresence');
      hadLocal = (lv !== null);
      savedCam = lv === '1';
    } catch (e) {}
    cameraPresenceEl.checked = savedCam;
    if (savedCam && typeof EvaCamera !== 'undefined' && EvaCamera) {
      EvaCamera.enable();
    }
    // If localStorage had no value (e.g. wiped on an app rebuild), fall back to
    // the bridge-persisted preference so the user does not re-enable each restart.
    if (!hadLocal) {
      try {
        var _pbase = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
        if (_pbase) {
          fetch(_pbase.replace(/\/+$/, '') + '/v1/prefs').then(function (r) {
            return r.ok ? r.json() : null;
          }).then(function (p) {
            if (p && p.cameraPresence === true) {
              cameraPresenceEl.checked = true;
              try { localStorage.setItem('cameraPresence', '1'); } catch (e) {}
              if (typeof EvaCamera !== 'undefined' && EvaCamera) EvaCamera.enable();
            }
          }).catch(function () {});
        }
      } catch (e) {}
    }
    cameraPresenceEl.addEventListener('change', function () {
      if (typeof EvaCamera === 'undefined' || !EvaCamera) return;
      var on = cameraPresenceEl.checked;
      // Persist to the bridge too so the choice survives a localStorage wipe.
      try {
        var _b = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
        if (_b) {
          fetch(_b.replace(/\/+$/, '') + '/v1/prefs', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cameraPresence: on })
          }).catch(function () {});
        }
      } catch (e) {}
      if (on) {
        EvaCamera.enable().then(function (ok) {
          if (!ok) cameraPresenceEl.checked = false;
        });
      } else {
        EvaCamera.disable();
      }
    });
  }

  // Vision provider for camera "looks" (GitHub-hosted / OpenAI direct / Copilot).
  // Persisted to the same localStorage key the camera module reads.
  var cameraVisionProviderEl = document.getElementById('cameraVisionProvider');
  if (cameraVisionProviderEl) {
    var savedProvider = '';
    try { savedProvider = localStorage.getItem('cameraVisionProvider') || ''; } catch (e) {}
    if (savedProvider) {
      var hasProv = Array.from(cameraVisionProviderEl.options).some(function (o) { return o.value === savedProvider; });
      if (hasProv) cameraVisionProviderEl.value = savedProvider;
    }
    cameraVisionProviderEl.addEventListener('change', function () {
      try { localStorage.setItem('cameraVisionProvider', cameraVisionProviderEl.value); } catch (e) {}
    });
  }

  function toggleSettings(event) {
    event.stopPropagation();
    var overlay = document.getElementById('settingsOverlay');
    var isOpen = settingsMenu.classList.contains('open');
    if (isOpen) {
      settingsMenu.classList.remove('open');
      if (overlay) overlay.classList.remove('open');
    } else {
      settingsMenu.classList.add('open');
      if (overlay) overlay.classList.add('open');
      populateAuthFields();
      if (typeof loadBackgroundData === 'function') loadBackgroundData(true);
      if (typeof loadDataMode === 'function') loadDataMode();
    }
  }

  // Attach event via JavaScript
  settingsButton.addEventListener('click', toggleSettings);

  // Mirror: sidebar Settings should toggle the same menu
  if (sidebarSettingsBtn) {
    sidebarSettingsBtn.addEventListener('click', toggleSettings);
  }
  // Mirror: sidebar Clear -> Clear Messages
  if (sidebarClearBtn) {
    sidebarClearBtn.addEventListener('click', (e) => { e.stopPropagation(); clearMessages(); });
  }

  // Close the menu when clicking outside
  document.addEventListener('click', (event) => {
    if (!settingsMenu.contains(event.target) && event.target !== settingsButton) {
      settingsMenu.classList.remove('open');
      var overlay = document.getElementById('settingsOverlay');
      if (overlay) overlay.classList.remove('open');
    }
  });

  // Initialize theme from localStorage
  try {
    const savedTheme = (function() {
      var t = localStorage.getItem('theme') || 'eva';
      if (t === 'default') t = 'legacy'; // migrate old "default" theme name
      return t;
    })();
  const savedCollapsed = localStorage.getItem('lcars_collapsed') === '1';
    if (themeSelect) {
      themeSelect.value = savedTheme;
    }
  // Capture full model list before any theme-based filtering
  captureOriginalModelOptions();
    applyTheme(savedTheme);
  // Ensure model options reflect the saved theme on load
  updateModelOptionsForTheme(savedTheme);
    var _isEvaInit = (savedTheme === 'eva' || (savedTheme && savedTheme.indexOf('eva-') === 0));
    // Apply collapsed state if saved (LCARS or Eva use the sidebar)
    if ((savedTheme === 'lcars' || _isEvaInit) && savedCollapsed) {
      document.body.classList.add('lcars-collapsed');
    }
    // Move Speak button into sidebar if active (LCARS or Eva)
    if ((savedTheme === 'lcars' || _isEvaInit) && lcarsChipSand && speakBtn && !lcarsChipSand.contains(speakBtn)) {
      lcarsChipSand.appendChild(speakBtn);
      speakBtn.title = 'Speak';
      speakBtn.textContent = 'Speak';
    }
    // Move Print button under Speak (LCARS or Eva)
    if ((savedTheme === 'lcars' || _isEvaInit) && lcarsChipPrint && printBtn && !lcarsChipPrint.contains(printBtn)) {
      lcarsChipPrint.appendChild(printBtn);
      printBtn.title = 'Print Output';
    }
    // Update LCARS label with current date
    if (lcarsLabel) {
      const now = new Date();
      const dateStr = now.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' });
      lcarsLabel.textContent = `Access • ${dateStr}`;
    }
  } catch (e) {
    console.warn('Theme init failed:', e);
  }

  // Toggle LCARS sidebar collapse on top chip click
  if (lcarsChipTop) {
    lcarsChipTop.setAttribute('role', 'button');
    lcarsChipTop.setAttribute('tabindex', '0');
    // Helper to sync tooltip title only
    function syncHandleTooltip() {
      var collapsed = document.body.classList.contains('lcars-collapsed');
      lcarsChipTop.title = collapsed ? 'Expand LCARS sidebar' : 'Collapse LCARS sidebar';
    }
    syncHandleTooltip();
    lcarsChipTop.addEventListener('click', function(e){
      e.stopPropagation();
      document.body.classList.toggle('lcars-collapsed');
      try { localStorage.setItem('lcars_collapsed', document.body.classList.contains('lcars-collapsed') ? '1' : '0'); } catch (e) {}
      syncHandleTooltip();
    });
    lcarsChipTop.addEventListener('keydown', function(e){
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        lcarsChipTop.click();
      }
    });
  }

  // Eva New Chat button — clear chat and restore welcome MOTD
  var evaNewChat = document.getElementById('evaNewChatBtn');
  if (evaNewChat) {
    evaNewChat.addEventListener('click', function() {
      if (typeof clearMessages === 'function') clearMessages();
      restoreEvaWelcome();
    });
  }

  // Eva sidebar nav buttons — open settings with correct tab
  // Must stopPropagation so the document click-outside handler doesn't immediately close settings
  function evaOpenSettings(e, tabName) {
    e.stopPropagation();
    var overlay = document.getElementById('settingsOverlay');
    if (!settingsMenu.classList.contains('open')) {
      settingsMenu.classList.add('open');
      if (overlay) overlay.classList.add('open');
      populateAuthFields();
      if (typeof loadBackgroundData === 'function') loadBackgroundData(true);
    }
    if (tabName) {
      setTimeout(function() {
        var tab = document.querySelector('[data-stab=' + tabName + ']');
        if (tab) tab.click();
      }, 50);
    }
  }
  var evaPromptsBtn = document.getElementById('evaPromptsBtn');
  if (evaPromptsBtn) evaPromptsBtn.addEventListener('click', function(e) { evaOpenSettings(e, 'prompts'); });
  var evaModelsBtn = document.getElementById('evaModelsBtn');
  if (evaModelsBtn) evaModelsBtn.addEventListener('click', function(e) { evaOpenSettings(e, 'models'); });
  var evaSettingsBtn = document.getElementById('evaSettingsBtn');
  if (evaSettingsBtn) evaSettingsBtn.addEventListener('click', function(e) { evaOpenSettings(e, null); });
  var evaUserBtn = document.getElementById('evaUserBtn');
  if (evaUserBtn) evaUserBtn.addEventListener('click', function(e) { evaOpenSettings(e, 'auth'); });
  var evaInputGear = document.getElementById('evaInputSettings');
  if (evaInputGear) evaInputGear.addEventListener('click', function(e) { evaOpenSettings(e, null); });

  // Monitors: tab switching
  if (monitorTabs && monitorPanels) {
    monitorTabs.addEventListener('click', function(e){
      const btn = e.target.closest('.monitor-tab');
      if (!btn) return;
      const tab = btn.getAttribute('data-tab');
      monitorTabs.querySelectorAll('.monitor-tab').forEach(b=>{
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
      });
      monitorPanels.querySelectorAll('.monitor-panel').forEach(p=>{
        const match = p.getAttribute('data-tab') === tab;
        p.classList.toggle('active', match);
        p.setAttribute('aria-hidden', match ? 'false' : 'true');
      });
    });
  }

  // Settings panel tab switching
  var settingsTabs = document.querySelectorAll('.settings-tab');
  var settingsPanels = document.querySelectorAll('.settings-panel');
  settingsTabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      var target = tab.getAttribute('data-stab');
      settingsTabs.forEach(function(t) {
        t.classList.toggle('active', t === tab);
      });
      settingsPanels.forEach(function(p) {
        p.classList.toggle('active', p.getAttribute('data-stab') === target);
      });
      if (target === 'goals' && typeof loadGoals === 'function') loadGoals(false);
      if (target === 'background' && typeof loadBackgroundData === 'function') loadBackgroundData(false);
    });
  });

  // Settings close button
  var settingsCloseBtn = document.getElementById('settingsClose');
  if (settingsCloseBtn) {
    settingsCloseBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      settingsMenu.classList.remove('open');
      var overlay = document.getElementById('settingsOverlay');
      if (overlay) overlay.classList.remove('open');
    });
  }

  // Settings overlay click to close
  var settingsOverlayEl = document.getElementById('settingsOverlay');
  if (settingsOverlayEl) {
    settingsOverlayEl.addEventListener('click', function() {
      settingsMenu.classList.remove('open');
      settingsOverlayEl.classList.remove('open');
    });
  }

  ['aigLmStudioBaseUrl', 'aigLmStudioModel', 'authSignalSender', 'authSignalRecipient'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', saveAuthKeys);
  });

  // Init auth, system prompt, and model settings
  loadAuthOverrides();
  populateAuthFields();
  initSystemPrompt();
  onModelSettingsChange();
  // Now enable auto-switch for user-initiated model changes and seed
  // the selector from the bridge (source of truth for persisted mode).
  _modeInitDone = true;
  if (typeof loadDataMode === 'function') loadDataMode();
  if (typeof cogInit === 'function') cogInit();
  if (typeof cogUpdatePromptsTabUI === 'function') cogUpdatePromptsTabUI();
  if (typeof initGoals === 'function') initGoals();
  if (typeof initBackground === 'function') initBackground();
  if (typeof initAlerts === 'function') initAlerts();
  if (typeof initSkills === 'function') initSkills();
  if (typeof initNotifications === 'function') initNotifications();

  // Initialize status panel with any pending config/init notes
  setStatus('info', document.getElementById('idText') && document.getElementById('idText').textContent ? document.getElementById('idText').textContent : '');

  // Global error handlers -> footer status
  window.addEventListener('error', function(ev){
    try {
      setStatus('error', (ev && ev.message) ? ev.message : 'An error occurred');
    } catch(_){}
  });
  window.addEventListener('unhandledrejection', function(ev){
    try {
      var msg = (ev && ev.reason && (ev.reason.message || ev.reason)) ? (ev.reason.message || String(ev.reason)) : 'Unhandled promise rejection';
      setStatus('error', msg);
    } catch(_){}
  });
});

// Welcome Text
/** Welcome message shown on new/empty sessions */
function showWelcome() {
  var txtOutput = document.getElementById('txtOutput');
  if (!txtOutput) return;
  txtOutput.innerHTML =
    '<div class="chat-bubble eva-bubble">' +
    '<span class="eva">Eva:</span> ' +
    'Welcome back. Here\'s what I can do:<br><br>' +
    '&bull; <b>Persistent Memory</b> &mdash; I remember your preferences, facts, and past conversations across sessions.<br>' +
    '&bull; <b>Voice Activation</b> &mdash; Click <b>Mic</b> and say <b>"Eva"</b> followed by your question. I\'ll listen quietly until you call.<br>' +
    '&bull; <b>Sessions</b> &mdash; Your conversations auto-save. Use <b>Sessions</b> to switch or start fresh.<br>' +
    '&bull; <b>Live Data</b> &mdash; Ask about weather, news, stocks, or space weather for real-time info.<br>' +
    '&bull; <b>Image Search &amp; Generation</b> &mdash; Ask me to show or generate an image of anything.<br>' +
    '&bull; <b>Multiple Models</b> &mdash; Switch providers in Settings &rarr; Models (OpenAI, Gemini, Copilot, local LLMs).<br><br>' +
    'Just type or speak &mdash; I\'m ready.' +
    '</div>';
}

// ═══════════════════════════════════════════════════════════════
//  Voice View — ambient, always-listening mode (sci-fi HUD)
// ═══════════════════════════════════════════════════════════════

var _vv = {
  open: false,
  animFrame: null,
  waveFrame: null,
  audioCtx: null,
  analyser: null,
  micStream: null,
  dataArray: null,
  phase: 'idle', // idle | listening | awake | thinking | speaking | error
  recognition: null,
  awakeTimer: null,
  convoMode: true,        // stay in an active conversation after the wake word
  convoTimeoutMs: 30000,  // quiet period before dropping back to standby
  lastTranscript: '',
  lastEvaReply: '',
  speakObserver: null,
  ttsSource: null,
  ttsAnalyser: null,
  ttsDataArray: null,
  ttsDelay: null,
  particles: [],
  hudInterval: null,
  cmdStart: 0
};

function toggleVoiceView() {
  if (_vv.open) {
    closeVoiceView();
  } else {
    openVoiceView();
  }
}

function openVoiceView() {
  var el = document.getElementById('voiceView');
  if (!el) return;
  _vv.open = true;
  el.classList.add('open');
  el.setAttribute('aria-hidden', 'false');
  // Conversation mode: after the wake word, keep listening between turns until a
  // quiet period elapses. Persisted so the user's choice survives restarts.
  try {
    _vv.convoMode = localStorage.getItem('vvConvoMode') !== '0';
    var savedTimeout = parseInt(localStorage.getItem('vvConvoTimeoutMs'), 10);
    if (Number.isInteger(savedTimeout) && savedTimeout >= 5000 && savedTimeout <= 300000) {
      _vv.convoTimeoutMs = savedTimeout;
    }
  } catch (e) {}
  _vvSyncConvoControls();
  _vvSetStatus('idle');

  var closeBtn = document.getElementById('voiceViewClose');
  if (closeBtn) closeBtn.onclick = closeVoiceView;

  var assetsClose = document.getElementById('vvAssetsClose');
  if (assetsClose) assetsClose.onclick = _vvHideAssets;

  var canvas = document.getElementById('voiceViewCanvas');
  if (canvas) canvas.onclick = _vvToggleListening;

  _vv._onEscape = function(e) { if (e.key === 'Escape') closeVoiceView(); };
  document.addEventListener('keydown', _vv._onEscape);

  _vvInitParticles();
  _vvStartCanvas();
  _vvStartWaveBar();
  _vvStartHUD();
  _vvStartLogStream();
}

function closeVoiceView() {
  _vv.open = false;
  var el = document.getElementById('voiceView');
  if (el) {
    el.classList.remove('open');
    el.setAttribute('aria-hidden', 'true');
    el.removeAttribute('data-phase');
  }
  if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
  _vvDetachSpeakStartListeners();
  if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
  if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }
  _vvStopBargeMonitor();
  _vvStopLogStream();
  // Clear the embedded vision panel so a stale frame does not linger on reopen.
  var vvVision = document.getElementById('vvVision');
  if (vvVision) {
    vvVision.classList.remove('open', 'looking');
    vvVision.setAttribute('aria-hidden', 'true');
    var vvShot = document.getElementById('vvVisionShot');
    if (vvShot) vvShot.removeAttribute('src');
    var vvText = document.getElementById('vvVisionText');
    if (vvText) vvText.textContent = '';
  }
  if (_vv._wasAutoSpeak !== undefined) {
    var autoSpeak = document.getElementById('autoSpeak');
    if (autoSpeak) autoSpeak.checked = _vv._wasAutoSpeak;
    delete _vv._wasAutoSpeak;
  }
  if (_vv._onEscape) {
    document.removeEventListener('keydown', _vv._onEscape);
    delete _vv._onEscape;
  }
  _vvStopListening();
  _vvStopCanvas();
  _vvStopWaveBar();
  _vvStopHUD();
  _vvHideAssets();
}

function _vvSetStatus(phase) {
  _vv.phase = phase;
  var el = document.getElementById('voiceView');
  if (el) el.setAttribute('data-phase', phase);
  // Update HUD phase indicator
  var ph = document.getElementById('vvHudPhase');
  if (ph) {
    var labels = { idle: 'IDLE', listening: 'LISTENING', awake: 'AWAKE', thinking: 'PROCESSING', speaking: 'SPEAKING', error: 'ERROR' };
    ph.textContent = labels[phase] || phase.toUpperCase();
  }
}

// --- Particle system ---

function _vvInitParticles() {
  _vv.particles = [];
  for (var i = 0; i < 60; i++) {
    _vv.particles.push({
      angle: Math.random() * Math.PI * 2,
      dist: 0.5 + Math.random() * 0.6,
      speed: 0.1 + Math.random() * 0.3,
      size: 0.5 + Math.random() * 1.5,
      alpha: 0.1 + Math.random() * 0.3,
      drift: (Math.random() - 0.5) * 0.02
    });
  }
  // Electrical impulse pools: radial discharges shoot outward from the orb edge
  // like neural firings; orbit pulses race along the outer rings leaving a
  // glowing trail. Both are spawned dynamically in the draw loop.
  _vv.impulses = [];
  _vv.orbits = [];
  _vv._lastImpulseT = 0;
}

// --- HUD data feeds ---

function _vvStartHUD() {
  _vvUpdateHUD();
  _vv.hudInterval = setInterval(_vvUpdateHUD, 1000);
}

function _vvStopHUD() {
  if (_vv.hudInterval) { clearInterval(_vv.hudInterval); _vv.hudInterval = null; }
}

// --- Background log feed (faint scrolling bridge stdout) ---

function _vvStartLogStream() {
  var el = document.getElementById('vvLogStream');
  if (!el) return;
  el.innerHTML = '';
  _vv._logSince = 0;
  _vv._logPolling = false;
  _vvPollLogStream();
  _vv.logInterval = setInterval(_vvPollLogStream, 15000);
}

function _vvStopLogStream() {
  if (_vv.logInterval) { clearInterval(_vv.logInterval); _vv.logInterval = null; }
  var el = document.getElementById('vvLogStream');
  if (el) el.innerHTML = '';
}

async function _vvPollLogStream() {
  if (!_vv.open || _vv._logPolling) return;
  _vv._logPolling = true;
  try {
    var base = (typeof getSafeBridgeBaseUrl === 'function') ? getSafeBridgeBaseUrl() : '';
    if (!base) return;
    var opts = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) opts.signal = AbortSignal.timeout(2500);
    var resp = await fetch(base.replace(/\/+$/, '') + '/v1/logs?since=' + (_vv._logSince || 0) + '&limit=40', opts);
    if (!resp.ok) return;
    var data = await resp.json();
    var lines = (data && Array.isArray(data.lines)) ? data.lines : [];
    if (typeof data.last === 'number') _vv._logSince = data.last;
    if (!lines.length) return;
    var el = document.getElementById('vvLogStream');
    if (!el) return;
    lines.forEach(function (ln) {
      var div = document.createElement('div');
      div.className = 'vv-log-line';
      div.textContent = String(ln.text || '');
      el.appendChild(div);
    });
    // Cap the rendered backlog so the small corner box stays light.
    while (el.childNodes.length > 24) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  } catch (_) {
    // Bridge unreachable or logs unavailable; stay quiet.
  } finally {
    _vv._logPolling = false;
  }
}

function _vvUpdateHUD() {
  // Model
  var modelEl = document.getElementById('vvHudModel');
  if (modelEl) {
    var sel = document.getElementById('selModel');
    var modelName = sel ? (sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].text : sel.value) : '--';
    if (modelName.length > 16) modelName = modelName.substring(0, 14) + '..';
    modelEl.textContent = modelName;
  }
  // Signal level from mic
  var sigEl = document.getElementById('vvHudSignal');
  if (sigEl) {
    if (_vv.analyser && _vv.dataArray && (_vv.phase === 'listening' || _vv.phase === 'awake')) {
      _vv.analyser.getByteFrequencyData(_vv.dataArray);
      var sum = 0;
      for (var i = 0; i < _vv.dataArray.length; i++) sum += _vv.dataArray[i];
      var avg = sum / _vv.dataArray.length;
      var db = Math.round(20 * Math.log10(Math.max(avg, 1) / 255));
      sigEl.textContent = db + ' dB';
    } else {
      sigEl.textContent = '--';
    }
  }
  // Latency
  var latEl = document.getElementById('vvHudLatency');
  if (latEl) {
    if (_vv.phase === 'thinking' && _vv.cmdStart) {
      latEl.textContent = Math.round(performance.now() - _vv.cmdStart) + ' ms';
    } else if (typeof _netStats !== 'undefined' && _netStats.lastLatency) {
      latEl.textContent = _netStats.lastLatency + ' ms';
    } else {
      latEl.textContent = '-- ms';
    }
  }
  // Live token telemetry replaces the lower-screen hint tidbit
  var telEl = document.getElementById('vvHudTelemetry');
  if (telEl) {
    var ctxTokens = 0, msgCount = 0;
    try { ctxTokens = computeMessagesTokens() || 0; } catch (e) { ctxTokens = 0; }
    try { msgCount = _countAllMessages() || 0; } catch (e) { msgCount = 0; }
    if (ctxTokens > 0 || msgCount > 0) {
      var ctxStr = ctxTokens >= 1000 ? (ctxTokens / 1000).toFixed(1) + 'k' : String(ctxTokens);
      var parts = ['CTX ' + ctxStr + ' tok', msgCount + ' msg'];
      if (typeof _netStats !== 'undefined') {
        parts.push('REQ ' + (_netStats.requests || 0));
        if (_netStats.errors) parts.push('ERR ' + _netStats.errors);
        if (_netStats.lastProvider) parts.push(String(_netStats.lastProvider).toUpperCase());
      }
      telEl.innerHTML = parts.join(' &middot; ');
    } else {
      telEl.innerHTML = 'tap orb to listen &middot; say <em>Eva</em> to wake';
    }
  }
}

// --- Main orb canvas ---

function _vvStartCanvas() {
  var canvas = document.getElementById('voiceViewCanvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;
  var cx = w / 2, cy = h / 2, baseR = w * 0.28;

  function draw() {
    if (!_vv.open) return;
    ctx.clearRect(0, 0, w, h);

    if (!_vv.impulses) _vv.impulses = [];
    if (!_vv.orbits) _vv.orbits = [];

    var t = performance.now() / 1000;
    var phase = _vv.phase;

    // Audio data
    var freqData = null;
    if (_vv.analyser && _vv.dataArray) {
      _vv.analyser.getByteFrequencyData(_vv.dataArray);
      freqData = _vv.dataArray;
    }
    var ttsData = null;
    if (_vv.ttsAnalyser && _vv.ttsDataArray && phase === 'speaking') {
      _vv.ttsAnalyser.getByteFrequencyData(_vv.ttsDataArray);
      ttsData = _vv.ttsDataArray;
    }
    var activeData = ttsData || freqData;

    // Band energies drive organic motion and impulse spawning. Voice lives in
    // the low/mid bins, so we weight those for the overall level.
    var bass = 0, mid = 0, treble = 0, level = 0;
    if (activeData && activeData.length) {
      var an = activeData.length;
      var bEnd = Math.max(1, Math.floor(an * 0.12));
      var mEnd = Math.max(bEnd + 1, Math.floor(an * 0.45));
      var sb = 0; for (var bb = 0; bb < bEnd; bb++) sb += activeData[bb];
      var sm = 0; for (var bm = bEnd; bm < mEnd; bm++) sm += activeData[bm];
      var st = 0; for (var bt = mEnd; bt < an; bt++) st += activeData[bt];
      bass = sb / bEnd / 255;
      mid = sm / (mEnd - bEnd) / 255;
      treble = st / (an - mEnd) / 255;
      level = bass * 0.6 + mid * 0.32 + treble * 0.08;
    }

    // Phase colors
    var hue, sat, light, glowAlpha, ringHue;
    if (phase === 'awake')      { hue = 270; sat = 75; light = 65; glowAlpha = 0.6; ringHue = 265; }
    else if (phase === 'thinking') { hue = 210; sat = 80; light = 60; glowAlpha = 0.5; ringHue = 200; }
    else if (phase === 'speaking') { hue = 155; sat = 65; light = 55; glowAlpha = 0.55; ringHue = 145; }
    else if (phase === 'listening'){ hue = 250; sat = 55; light = 50; glowAlpha = 0.35; ringHue = 240; }
    else if (phase === 'error')    { hue = 0; sat = 60; light = 50; glowAlpha = 0.35; ringHue = 350; }
    else                           { hue = 220; sat = 25; light = 35; glowAlpha = 0.15; ringHue = 215; }

    var col = function(h, s, l, a) { return 'hsla(' + h + ',' + s + '%,' + l + '%,' + a + ')'; };

    // === Background radial glow ===
    var bgGrad = ctx.createRadialGradient(cx, cy, baseR * 0.3, cx, cy, baseR * 2.5);
    bgGrad.addColorStop(0, col(hue, sat, light, glowAlpha * 0.15));
    bgGrad.addColorStop(0.5, col(hue, sat, light * 0.5, glowAlpha * 0.05));
    bgGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, w, h);

    // === Outer ring 3 (thin, far, slow rotate) ===
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.08);
    ctx.beginPath();
    var r3 = baseR * 1.7;
    for (var i = 0; i < 72; i++) {
      var a = (i / 72) * Math.PI * 2;
      var gap = (i % 6 === 0) ? 0.3 : 1;
      if (gap < 1) continue;
      var x1 = Math.cos(a) * (r3 - 1), y1 = Math.sin(a) * (r3 - 1);
      var x2 = Math.cos(a) * (r3 + 1), y2 = Math.sin(a) * (r3 + 1);
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = col(ringHue, 35, 32, 0.04);
    ctx.lineWidth = 0.5;
    ctx.stroke();
    ctx.restore();

    // === Outer ring 2 (dashed, counter-rotate) ===
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-t * 0.15);
    ctx.beginPath();
    ctx.setLineDash([8, 16]);
    ctx.arc(0, 0, baseR * 1.45, 0, Math.PI * 2);
    ctx.strokeStyle = col(ringHue, 35, 32, 0.05);
    ctx.lineWidth = 0.8;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    // === Outer ring 1 (solid, subtle) ===
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.25);
    ctx.beginPath();
    ctx.arc(0, 0, baseR * 1.2, 0, Math.PI * 2);
    ctx.strokeStyle = col(ringHue, sat, light * 0.55, 0.06);
    ctx.lineWidth = 1;
    ctx.stroke();
    // Tick marks every 30 deg
    for (var d = 0; d < 12; d++) {
      var ta = (d / 12) * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(Math.cos(ta) * baseR * 1.18, Math.sin(ta) * baseR * 1.18);
      ctx.lineTo(Math.cos(ta) * baseR * 1.24, Math.sin(ta) * baseR * 1.24);
      ctx.strokeStyle = col(ringHue, sat, light * 0.55, 0.1);
      ctx.lineWidth = d % 3 === 0 ? 1.5 : 0.7;
      ctx.stroke();
    }
    ctx.restore();

    // === Scanning beam (thinking/awake only) ===
    if (phase === 'thinking' || phase === 'awake') {
      ctx.save();
      ctx.translate(cx, cy);
      var sweepAngle = (t * 1.5) % (Math.PI * 2);
      var sweepGrad = ctx.createConicGradient(sweepAngle, 0, 0);
      sweepGrad.addColorStop(0, col(hue, sat, light, 0.25));
      sweepGrad.addColorStop(0.15, 'transparent');
      sweepGrad.addColorStop(1, 'transparent');
      ctx.beginPath();
      ctx.arc(0, 0, baseR * 1.15, 0, Math.PI * 2);
      ctx.fillStyle = sweepGrad;
      ctx.fill();
      ctx.restore();
    }

    // === Particles ===
    for (var pi = 0; pi < _vv.particles.length; pi++) {
      var p = _vv.particles[pi];
      p.angle += p.speed * 0.008;
      p.dist += p.drift * 0.005;
      if (p.dist < 0.35 || p.dist > 1.3) p.drift = -p.drift;
      var pr = baseR * p.dist * 1.6;
      var px = cx + Math.cos(p.angle + t * 0.1) * pr;
      var py = cy + Math.sin(p.angle + t * 0.1) * pr;
      var pa = p.alpha * (0.5 + 0.5 * Math.sin(t * 2 + pi));
      ctx.beginPath();
      ctx.arc(px, py, p.size, 0, Math.PI * 2);
      ctx.fillStyle = col(hue, sat - 10, light + 20, pa);
      ctx.fill();
    }

    // === Main waveform orb ===
    // The perimeter is deformed by three overlapping influences so the WHOLE
    // ring stays alive (no flat arc): (1) traveling harmonic ripples that orbit
    // the circle continuously, (2) audio energy that is itself rotated around
    // the ring over time so loud bins sweep around instead of pinning to a
    // fixed angle, and (3) a gentle breath. This reads as an organic, living
    // membrane rather than a static spectrum readout.
    var segments = 180;
    var dataLen = (activeData && activeData.length) ? activeData.length : 0;
    var usableBins = dataLen ? Math.max(1, Math.floor(dataLen * 0.6)) : 0;
    var swirl = t * 0.18; // audio sweep rate around the ring
    ctx.beginPath();
    for (var si = 0; si <= segments; si++) {
      var angle = (si / segments) * Math.PI * 2 - Math.PI / 2;
      var pos = si / segments;

      // Audio energy, rotated around the ring and reflected so there is no seam.
      var amp = 0;
      if (usableBins > 0) {
        var swept = pos + swirl;
        var frac = swept - Math.floor(swept);          // 0..1 wrapped
        var refl = frac <= 0.5 ? frac * 2 : (1 - frac) * 2; // 0..1..0, seamless
        var fi = Math.min(usableBins - 1, Math.floor(refl * usableBins));
        amp = (activeData[fi] / 255) * (0.22 + level * 0.5);
      }

      // Traveling harmonic ripples. Different speeds/directions keep every part
      // of the ring in motion; amplitude swells with audio level but never goes
      // fully flat, so the orb always breathes.
      var organic =
        Math.sin(angle * 3 + t * 1.6) * 0.55 +
        Math.sin(angle * 5 - t * 1.1) * 0.30 +
        Math.sin(angle * 8 + t * 2.4) * 0.18 +
        Math.sin(angle * 13 - t * 3.1) * 0.10;
      organic *= (0.022 + level * 0.085 + bass * 0.04);

      var breathe = Math.sin(t * 1.2) * 0.012;
      var pulse = (phase === 'awake' || phase === 'speaking') ? Math.sin(t * 4) * 0.018 : 0;
      var think = (phase === 'thinking') ? Math.sin(t * 5 + angle * 9) * 0.03 : 0;

      var r = baseR * (1 + amp * 0.4 + organic + breathe + pulse + think);
      var x = cx + Math.cos(angle) * r;
      var y = cy + Math.sin(angle) * r;
      if (si === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();

    // Orb fill
    var fillGrad = ctx.createRadialGradient(cx, cy - baseR * 0.2, 0, cx, cy, baseR * 1.3);
    fillGrad.addColorStop(0, col(hue, sat, light + 20, 0.1));
    fillGrad.addColorStop(0.5, col(hue, sat, light, 0.04));
    fillGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = fillGrad;
    ctx.fill();

    // Orb stroke (double glow)
    ctx.strokeStyle = col(hue, sat, light, 0.5 + glowAlpha * 0.4);
    ctx.lineWidth = 1.5;
    ctx.shadowColor = col(hue, sat, light, glowAlpha);
    ctx.shadowBlur = 24;
    ctx.stroke();
    ctx.shadowColor = col(hue, sat, light + 10, glowAlpha * 0.5);
    ctx.shadowBlur = 60;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // === Electrical impulses ===
    // Two effects layered for a "the future is now" feel:
    //   1. Radial discharges: jagged lightning that fires outward from the orb
    //      surface, like synapses or energy arcing into the field.
    //   2. Orbit pulses: bright sparks that race along the outer rings leaving a
    //      fading comet trail, so energy is always traveling through the window.
    // Spawn rates scale with the phase and live audio level.
    var dt = Math.min(0.05, Math.max(0.001, t - (_vv._lastT || t)));
    _vv._lastT = t;
    var activity = phase === 'speaking' ? (0.2 + level * 1.1)
                 : phase === 'thinking' ? 0.32
                 : phase === 'awake' ? 0.16
                 : phase === 'listening' ? (0.07 + level * 0.5)
                 : phase === 'error' ? 0.12
                 : 0.03;

    // Spawn radial discharges.
    if (_vv.impulses.length < 16 && Math.random() < activity * 0.32) {
      var ia = Math.random() * Math.PI * 2;
      var nodes = 5 + Math.floor(Math.random() * 4);
      var offs = [];
      for (var ni = 0; ni < nodes; ni++) offs.push((Math.random() - 0.5));
      _vv.impulses.push({
        angle: ia,
        reach: 0.45 + Math.random() * 0.7,   // how far past the orb it travels
        prog: 0,
        speed: 1.6 + Math.random() * 1.8,
        offs: offs,
        width: 0.8 + Math.random() * 1.2
      });
    }
    // Spawn orbit pulses.
    if (_vv.orbits.length < 8 && Math.random() < activity * 0.2) {
      var ringR = (Math.random() < 0.5 ? 1.2 : 1.45) + (Math.random() - 0.5) * 0.1;
      _vv.orbits.push({
        angle: Math.random() * Math.PI * 2,
        radius: ringR,
        dir: Math.random() < 0.5 ? 1 : -1,
        speed: 1.4 + Math.random() * 1.8,
        prog: 0,
        life: 0.7 + Math.random() * 0.6
      });
    }

    // Draw + update radial discharges.
    for (var di = _vv.impulses.length - 1; di >= 0; di--) {
      var im = _vv.impulses[di];
      im.prog += dt * im.speed;
      if (im.prog >= 1) { _vv.impulses.splice(di, 1); continue; }
      var headLen = baseR * (0.04 + im.reach * im.prog);
      var startR = baseR * (1.0 + 0.02 * Math.sin(t * 4 + im.angle));
      var ca = Math.cos(im.angle), sa = Math.sin(im.angle);
      var px0 = cx + ca * startR, py0 = cy + sa * startR;
      var perpX = -sa, perpY = ca;
      var seg = im.offs.length;
      var fade = Math.sin(Math.PI * im.prog); // ramp in then out
      ctx.beginPath();
      ctx.moveTo(px0, py0);
      for (var ii = 0; ii < seg; ii++) {
        var frac2 = (ii + 1) / seg;
        var rr = startR + headLen * frac2;
        var jitter = im.offs[ii] * 10 * (1 - frac2) * (0.6 + level);
        var jx = cx + ca * rr + perpX * jitter;
        var jy = cy + sa * rr + perpY * jitter;
        ctx.lineTo(jx, jy);
      }
      ctx.strokeStyle = col(hue, sat - 5, light + 25, 0.5 * fade);
      ctx.lineWidth = im.width;
      ctx.shadowColor = col(hue, sat, light + 15, 0.7 * fade);
      ctx.shadowBlur = 12;
      ctx.stroke();
      // Bright head spark.
      var hx = cx + ca * (startR + headLen) + perpX * im.offs[seg - 1] * 4;
      var hy = cy + sa * (startR + headLen) + perpY * im.offs[seg - 1] * 4;
      ctx.beginPath();
      ctx.arc(hx, hy, im.width * 1.3, 0, Math.PI * 2);
      ctx.fillStyle = col(hue, sat - 15, 90, 0.8 * fade);
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    // Draw + update orbit pulses (comet trails along the outer rings).
    for (var oi = _vv.orbits.length - 1; oi >= 0; oi--) {
      var ob = _vv.orbits[oi];
      ob.prog += dt * (ob.speed / 6);
      if (ob.prog >= ob.life) { _vv.orbits.splice(oi, 1); continue; }
      var oFade = Math.sin(Math.PI * (ob.prog / ob.life));
      var orbR = baseR * ob.radius;
      var headA = ob.angle + ob.dir * ob.prog * 4.2;
      var trail = 14;
      for (var ti = 0; ti < trail; ti++) {
        var ta2 = headA - ob.dir * ti * 0.045;
        var tAlpha = oFade * (1 - ti / trail) * 0.6;
        if (tAlpha <= 0.01) continue;
        var tx = cx + Math.cos(ta2) * orbR;
        var ty = cy + Math.sin(ta2) * orbR;
        ctx.beginPath();
        ctx.arc(tx, ty, (1 - ti / trail) * 1.8 + 0.3, 0, Math.PI * 2);
        ctx.fillStyle = col(ringHue, sat, light + 25, tAlpha);
        ctx.fill();
      }
      // Bright head with glow.
      var ohx = cx + Math.cos(headA) * orbR;
      var ohy = cy + Math.sin(headA) * orbR;
      ctx.beginPath();
      ctx.arc(ohx, ohy, 2.2, 0, Math.PI * 2);
      ctx.fillStyle = col(ringHue, sat - 10, 92, 0.85 * oFade);
      ctx.shadowColor = col(ringHue, sat, light + 20, 0.8 * oFade);
      ctx.shadowBlur = 14;
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    // === Inner ring (heartbeat) ===
    var innerPulse = 0.55 + Math.sin(t * 2) * 0.02;
    ctx.beginPath();
    ctx.arc(cx, cy, baseR * innerPulse, 0, Math.PI * 2);
    ctx.strokeStyle = col(hue, sat, light, 0.07);
    ctx.lineWidth = 0.5;
    ctx.stroke();

    // === Center dot ===
    var dotR = 3 + Math.sin(t * 3) * 1;
    ctx.beginPath();
    ctx.arc(cx, cy, dotR, 0, Math.PI * 2);
    ctx.fillStyle = col(hue, sat, light + 20, 0.4 + glowAlpha * 0.3);
    ctx.shadowColor = col(hue, sat, light, 0.6);
    ctx.shadowBlur = 15;
    ctx.fill();
    ctx.shadowBlur = 0;

    // === Phase label under orb ===
    var phaseLabel = { idle: '', listening: 'LISTENING', awake: 'AWAKE', thinking: 'PROCESSING', speaking: 'SPEAKING', error: 'MIC ERROR' }[phase] || '';
    if (phaseLabel) {
      ctx.font = '600 10px "SF Mono", "Fira Code", monospace';
      ctx.textAlign = 'center';
      ctx.fillStyle = col(hue, sat, light, 0.4);
      ctx.letterSpacing = '3px';
      ctx.fillText(phaseLabel, cx, cy + baseR * 1.35);
    }

    _vv.animFrame = requestAnimationFrame(draw);
  }

  _vv.animFrame = requestAnimationFrame(draw);
}

function _vvStopCanvas() {
  if (_vv.animFrame) {
    cancelAnimationFrame(_vv.animFrame);
    _vv.animFrame = null;
  }
}

// --- Linear waveform bar (bottom HUD) ---

function _vvStartWaveBar() {
  var canvas = document.getElementById('vvWaveBar');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;

  function draw() {
    if (!_vv.open) return;
    ctx.clearRect(0, 0, w, h);

    var activeData = null;
    if (_vv.ttsAnalyser && _vv.ttsDataArray && _vv.phase === 'speaking') {
      _vv.ttsAnalyser.getByteFrequencyData(_vv.ttsDataArray);
      activeData = _vv.ttsDataArray;
    } else if (_vv.analyser && _vv.dataArray) {
      _vv.analyser.getByteFrequencyData(_vv.dataArray);
      activeData = _vv.dataArray;
    }

    var bars = 80;
    var barW = w / bars;
    var phase = _vv.phase;
    var hue = phase === 'speaking' ? 155 : phase === 'awake' ? 270 : phase === 'thinking' ? 210 : 220;
    var t = performance.now() / 1000;

    // Center baseline
    var mid = h / 2;

    for (var i = 0; i < bars; i++) {
      var val = 0;
      if (activeData && activeData.length > 0) {
        // Compress sampling toward the low/mid bins (where voice energy lives)
        // so the bars spread the motion across the whole bar instead of leaving
        // the high-frequency right side dead.
        var norm = i / bars;
        var curved = Math.pow(norm, 1.8);
        var fi = Math.floor(curved * activeData.length * 0.6);
        val = activeData[Math.min(activeData.length - 1, fi)] / 255;
      }
      // Always-alive traveling shimmer so the bar feels organic even in silence.
      var shimmer = (Math.sin(t * 1.6 - i * 0.22) * 0.5 + 0.5) * 0.04
                  + Math.abs(Math.sin(t * 0.8 + i * 0.15)) * 0.02;
      val = Math.max(val, shimmer);

      var barH = val * mid * 0.85;
      var x = i * barW + 1;
      var alpha = 0.15 + val * 0.6;

      ctx.fillStyle = 'hsla(' + hue + ',60%,55%,' + alpha + ')';
      ctx.fillRect(x, mid - barH, barW - 2, barH); // top half
      ctx.fillStyle = 'hsla(' + hue + ',60%,55%,' + alpha * 0.5 + ')';
      ctx.fillRect(x, mid, barW - 2, barH * 0.5); // mirror (dimmer)
    }

    // Center line
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(w, mid);
    ctx.strokeStyle = 'hsla(' + hue + ',50%,50%,0.08)';
    ctx.lineWidth = 0.5;
    ctx.stroke();

    _vv.waveFrame = requestAnimationFrame(draw);
  }

  _vv.waveFrame = requestAnimationFrame(draw);
}

function _vvStopWaveBar() {
  if (_vv.waveFrame) {
    cancelAnimationFrame(_vv.waveFrame);
    _vv.waveFrame = null;
  }
}

// --- Mic analyser ---

function _vvStartMicAnalyser() {
  if (_vv.analyser) return Promise.resolve();
  var AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return Promise.resolve();

  // Echo cancellation is what makes barge-in possible: it removes Eva's own TTS
  // output (played through the speakers) from the mic signal, so the energy the
  // barge monitor sees while she speaks is mostly the user, not Eva.
  var constraints = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } };
  return navigator.mediaDevices.getUserMedia(constraints).then(function(stream) {
    _vv.micStream = stream;
    if (!_vv.audioCtx) _vv.audioCtx = new AudioCtx();
    if (_vv.audioCtx.state === 'suspended') _vv.audioCtx.resume();
    var source = _vv.audioCtx.createMediaStreamSource(stream);
    _vv.analyser = _vv.audioCtx.createAnalyser();
    _vv.analyser.fftSize = 256;
    _vv.dataArray = new Uint8Array(_vv.analyser.frequencyBinCount);
    source.connect(_vv.analyser);
  }).catch(function(err) {
    console.warn('[VoiceView] Mic access denied:', err.message);
  });
}

function _vvStopMicAnalyser() {
  if (_vv.micStream) {
    _vv.micStream.getTracks().forEach(function(t) { t.stop(); });
    _vv.micStream = null;
  }
  if (_vv.audioCtx && _vv.audioCtx.state === 'running') {
    try { _vv.audioCtx.suspend(); } catch(e) {}
  }
  _vv.analyser = null;
  _vv.dataArray = null;
}

// --- TTS audio analyser ---

function _vvConnectTTSAnalyser() {
  _vvDisconnectTTSAnalyser();
  var audio = document.getElementById('audioPlayback');
  if (!audio) return;

  var AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;

  if (!_vv.audioCtx) _vv.audioCtx = new AudioCtx();
  if (_vv.audioCtx.state === 'suspended') _vv.audioCtx.resume();
  var ctx = _vv.audioCtx;

  try {
    // createMediaElementSource permanently reroutes the audio element into the
    // Web Audio graph. Connect it to the speakers FIRST so Eva is always
    // audible, even if the analyser wiring below fails. Without this ordering a
    // failure after the source is created leaves the element hijacked but with
    // no path to the speakers, which silences all TTS until a reload.
    if (!audio._vvSource) {
      audio._vvSource = ctx.createMediaElementSource(audio);
    }
    _vv.ttsSource = audio._vvSource;
    try { _vv.ttsSource.connect(ctx.destination); } catch (e) {}

    _vv.ttsAnalyser = ctx.createAnalyser();
    _vv.ttsAnalyser.fftSize = 256;
    // Lower smoothing than the 0.8 default so the bars track speech onsets and
    // pauses crisply instead of lagging behind and mushing together.
    _vv.ttsAnalyser.smoothingTimeConstant = 0.6;
    _vv.ttsDataArray = new Uint8Array(_vv.ttsAnalyser.frequencyBinCount);

    // The analyser taps the signal at the graph node, which is BEFORE the
    // device output latency (often 80-200ms on Linux/PulseAudio). Reading it
    // directly makes the waveform run AHEAD of the audio you hear. Route the
    // analyser branch (only) through a DelayNode set to the output latency so
    // the visualization lines up with the heard voice. The speaker path above
    // stays undelayed.
    var lat = ctx.outputLatency || ctx.baseLatency || 0;
    lat = Math.min(0.3, Math.max(0, lat));
    _vv.ttsDelay = ctx.createDelay(0.5);
    _vv.ttsDelay.delayTime.value = lat;
    _vv.ttsSource.connect(_vv.ttsDelay);
    _vv.ttsDelay.connect(_vv.ttsAnalyser);

    // outputLatency is often 0 until playback actually starts. Refine the
    // compensation once the device reports a real value.
    audio.addEventListener('playing', function _vvSyncDelay() {
      audio.removeEventListener('playing', _vvSyncDelay);
      if (!_vv.ttsDelay || !_vv.audioCtx) return;
      var rl = _vv.audioCtx.outputLatency || _vv.audioCtx.baseLatency || 0;
      rl = Math.min(0.3, Math.max(0, rl));
      try { _vv.ttsDelay.delayTime.value = rl; } catch (e) {}
    });
  } catch(e) {
    _vv.ttsAnalyser = null;
    _vv.ttsDataArray = null;
    _vv.ttsDelay = null;
    // Recovery: guarantee the element can still reach the speakers.
    try {
      if (audio._vvSource && _vv.audioCtx) audio._vvSource.connect(_vv.audioCtx.destination);
    } catch (e2) {}
  }
}

function _vvDisconnectTTSAnalyser() {
  // Tear down the analyser branch (source -> delay -> analyser). The speaker
  // path (source -> destination) is left intact so audio keeps playing.
  if (_vv.ttsSource && _vv.ttsDelay) {
    try { _vv.ttsSource.disconnect(_vv.ttsDelay); } catch(e) {}
  }
  if (_vv.ttsDelay && _vv.ttsAnalyser) {
    try { _vv.ttsDelay.disconnect(_vv.ttsAnalyser); } catch(e) {}
  }
  if (_vv.ttsSource && _vv.ttsAnalyser) {
    try { _vv.ttsSource.disconnect(_vv.ttsAnalyser); } catch(e) {}
  }
  _vv.ttsAnalyser = null;
  _vv.ttsDataArray = null;
  _vv.ttsDelay = null;
}

// --- Voice-mode asset surface ---

// Show images (or other media) in the voice view's asset window so the user
// can see what Eva surfaced without leaving the orb overlay.
function _vvSurfaceAssets(assets) {
  if (!assets || !assets.length) return;
  var panel = document.getElementById('vvAssets');
  var body = document.getElementById('vvAssetsBody');
  if (!panel || !body) return;

  body.innerHTML = '';
  assets.forEach(function(a) {
    if (!a || !a.url) return;
    var img = document.createElement('img');
    img.className = 'eva-inline-img';
    img.src = a.url;
    img.alt = a.caption || 'Image';
    body.appendChild(img);
    if (a.caption) {
      var cap = document.createElement('div');
      cap.className = 'vv-assets-caption';
      cap.textContent = a.generated ? a.caption + ' (AI generated)' : a.caption;
      body.appendChild(cap);
    }
  });

  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
}

function _vvHideAssets() {
  var panel = document.getElementById('vvAssets');
  var body = document.getElementById('vvAssetsBody');
  if (panel) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }
  if (body) body.innerHTML = '';
}


// --- Voice recognition ---

function _vvToggleListening() {
  if (_vv.recognition || _vv.whisperMode) {
    _vvStopListening();
  } else {
    _vvStartListening();
  }
}

function _vvStartListening() {
  if (window.evaStandalone && window.evaStandalone.isStandalone) {
    _vvStartWhisperListening();
    return;
  }

  var SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    _vvStartWhisperListening();
    return;
  }

  if (typeof stopVoiceListener === 'function') stopVoiceListener();

  _vvStartMicAnalyser();

  _vv.recognition = new SpeechRec();
  _vv.recognition.lang = 'en-US';
  _vv.recognition.continuous = true;
  _vv.recognition.interimResults = false;

  _vv.recognition.onstart = function() {
    _vvSetStatus('listening');
  };

  _vv.recognition.onresult = function(event) {
    _vv._lastResultTime = Date.now();
    for (var i = event.resultIndex; i < event.results.length; i++) {
      if (!event.results[i].isFinal) continue;
      _vvHandleTranscript(event.results[i][0].transcript.trim());
    }
  };

  _vv.recognition.onerror = function(event) {
    if (event.error === 'no-speech' || event.error === 'aborted') return;
    if (event.error === 'not-allowed') {
      _vvSetStatus('error');
      _vv.recognition = null;
      return;
    }
    if (event.error === 'network' || event.error === 'service-not-allowed') {
      console.warn('[VoiceView] Web Speech API unavailable (' + event.error + '), falling back to Whisper');
      _vv.recognition = null;
      _vvStartWhisperListening();
      return;
    }
  };

  _vv.recognition.onend = function() {
    if (_vv.recognition && _vv.open) {
      // Browser stopped recognition (normal after silence/timeout). Restart.
      _vv._restartAttempts = (_vv._restartAttempts || 0) + 1;
      var delay = Math.min(300 * _vv._restartAttempts, 2000);
      setTimeout(function() {
        if (!_vv.recognition || !_vv.open) return;
        try {
          _vv.recognition.start();
          _vv._restartAttempts = 0; // reset on success
        } catch(e) {
          console.warn('[VoiceView] Recognition restart failed (attempt ' + _vv._restartAttempts + '):', e);
          if (_vv._restartAttempts >= 5) {
            // Too many failures, fall back to Whisper if available
            console.warn('[VoiceView] Falling back to Whisper after repeated restart failures');
            _vv.recognition = null;
            _vv._restartAttempts = 0;
            if (typeof _vvStartWhisperListening === 'function') {
              _vvStartWhisperListening();
            } else {
              _vvSetStatus('error');
            }
          }
        }
      }, delay);
    }
  };

  _vv.recognition.start();
  _vv._lastResultTime = Date.now();
  _vv._restartAttempts = 0;

  // Watchdog: if no recognition results for 60s while the view is open and
  // not in speaking/thinking phase, force-restart recognition. The browser
  // sometimes silently stops delivering results without firing onend.
  if (_vv._watchdog) clearInterval(_vv._watchdog);
  _vv._watchdog = setInterval(function() {
    if (!_vv.open || !_vv.recognition) {
      clearInterval(_vv._watchdog);
      _vv._watchdog = null;
      return;
    }
    if (_vv.phase === 'speaking' || _vv.phase === 'thinking') return;
    var elapsed = Date.now() - (_vv._lastResultTime || Date.now());
    if (elapsed > 60000) {
      console.warn('[VoiceView] Watchdog: no results for 60s, restarting recognition');
      try { _vv.recognition.stop(); } catch(_) {}
      setTimeout(function() {
        if (_vv.recognition && _vv.open) {
          try { _vv.recognition.start(); _vv._lastResultTime = Date.now(); } catch(_) {}
        }
      }, 500);
    }
  }, 15000);
}

function _vvStopListening() {
  if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
  if (_vv.silenceTimer) { clearTimeout(_vv.silenceTimer); _vv.silenceTimer = null; }
  if (_vv.recordingCap) { clearTimeout(_vv.recordingCap); _vv.recordingCap = null; }
  if (_vv._watchdog) { clearInterval(_vv._watchdog); _vv._watchdog = null; }
  if (_vv._whisperWatchdog) { clearInterval(_vv._whisperWatchdog); _vv._whisperWatchdog = null; }
  if (_vv._energyMonitor) { clearInterval(_vv._energyMonitor); _vv._energyMonitor = null; }
  if (_vv.recognition) {
    var rec = _vv.recognition;
    _vv.recognition = null;
    try { rec.stop(); } catch(e) {}
  }
  if (_vv.mediaRecorder) {
    try { _vv.mediaRecorder.stop(); } catch(e) {}
    _vv.mediaRecorder = null;
  }
  _vv.whisperMode = false;
  _vv._whisperInflight = false;
  _vv.audioChunks = [];
  _vv.speechDetected = false;
  _vvStopMicAnalyser();
  _vvDisconnectTTSAnalyser();
  if (_vv.open) _vvSetStatus('idle');
}

// --- Whisper fallback ---

function _vvStartWhisperListening() {
  if (typeof stopVoiceListener === 'function') stopVoiceListener();

  _vv.whisperMode = true;
  _vv._whisperInflight = false;
  _vvSetStatus('listening');
  _vvStartMicAnalyser().then(function() {
    if (_vv.open && _vv.whisperMode) _vvWhisperRecord();
  });

  // Whisper recording loop watchdog: if the loop has stalled (no active
  // recorder and no API call in flight) while we should be listening, restart.
  if (_vv._whisperWatchdog) clearInterval(_vv._whisperWatchdog);
  _vv._whisperWatchdog = setInterval(function() {
    if (!_vv.open || !_vv.whisperMode) {
      clearInterval(_vv._whisperWatchdog); _vv._whisperWatchdog = null;
      return;
    }
    if (_vv.phase === 'thinking' || _vv.phase === 'speaking') return;
    var recActive = _vv.mediaRecorder && _vv.mediaRecorder.state === 'recording';
    if (!recActive && !_vv._whisperInflight) {
      console.warn('[VoiceView] Whisper watchdog: recording loop stalled, restarting');
      _vvWhisperRecord();
    }
  }, 10000);
}

function _vvWhisperRecord() {
  if (!_vv.open || !_vv.whisperMode || !_vv.micStream) return;
  if (_vv.phase === 'thinking' || _vv.phase === 'speaking') return;

  var mimeType = 'audio/webm';
  if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported) {
    if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mimeType = 'audio/webm;codecs=opus';
  }

  try {
    _vv.mediaRecorder = new MediaRecorder(_vv.micStream, { mimeType: mimeType });
  } catch(e) {
    _vvSetStatus('error');
    return;
  }

  _vv.audioChunks = [];
  _vv.speechDetected = false;

  _vv.mediaRecorder.ondataavailable = function(e) {
    if (e.data && e.data.size > 0) _vv.audioChunks.push(e.data);
  };

  _vv.mediaRecorder.onstop = function() {
    if (_vv.recordingCap) { clearTimeout(_vv.recordingCap); _vv.recordingCap = null; }
    if (!_vv.speechDetected || !_vv.audioChunks.length || !_vv.whisperMode) {
      if (_vv.open && _vv.whisperMode) setTimeout(function() { _vvWhisperRecord(); }, 200);
      return;
    }
    var blob = new Blob(_vv.audioChunks, { type: mimeType });
    _vv.audioChunks = [];
    _vvWhisperTranscribe(blob);
  };

  _vv.mediaRecorder.start(250);

  _vv.recordingCap = setTimeout(function() {
    _vv.recordingCap = null;
    if (_vv.mediaRecorder && _vv.mediaRecorder.state === 'recording') {
      try { _vv.mediaRecorder.stop(); } catch(e) {}
    }
  }, 30000);

  _vvWhisperMonitor();
}

function _vvWhisperMonitor() {
  if (!_vv.open || !_vv.whisperMode || !_vv.analyser || !_vv.dataArray) return;

  var threshold = 25;
  var silenceDelay = 1500;

  // Use setInterval instead of requestAnimationFrame so that Electron does not
  // throttle the energy monitor to 0 fps when the window is unfocused.
  if (_vv._energyMonitor) clearInterval(_vv._energyMonitor);
  _vv._energyMonitor = setInterval(function() {
    if (!_vv.open || !_vv.whisperMode || !_vv.analyser || !_vv.dataArray) {
      clearInterval(_vv._energyMonitor); _vv._energyMonitor = null; return;
    }
    if (_vv.phase === 'thinking' || _vv.phase === 'speaking') return;
    if (!_vv.mediaRecorder || _vv.mediaRecorder.state !== 'recording') return;

    _vv.analyser.getByteFrequencyData(_vv.dataArray);
    var sum = 0;
    for (var i = 0; i < _vv.dataArray.length; i++) sum += _vv.dataArray[i];
    var avg = sum / _vv.dataArray.length;

    if (avg > threshold) {
      _vv.speechDetected = true;
      if (_vv.silenceTimer) { clearTimeout(_vv.silenceTimer); _vv.silenceTimer = null; }
    } else if (_vv.speechDetected && !_vv.silenceTimer) {
      _vv.silenceTimer = setTimeout(function() {
        _vv.silenceTimer = null;
        if (_vv.mediaRecorder && _vv.mediaRecorder.state === 'recording') {
          try { _vv.mediaRecorder.stop(); } catch(e) {}
        }
      }, silenceDelay);
    }
  }, 100);
}

function _vvWhisperTranscribe(blob) {
  var apiKey = typeof getAuthKey === 'function' ? getAuthKey('OPENAI_API_KEY') : null;
  if (!apiKey) {
    _vvSetStatus('error');
    return;
  }

  _vv._whisperInflight = true;

  var formData = new FormData();
  formData.append('file', blob, 'audio.webm');
  formData.append('model', 'whisper-1');
  formData.append('language', 'en');

  // Abort controller with 20s timeout so a hanging network call does not
  // permanently kill the recording loop.
  var controller = new AbortController();
  var fetchTimeout = setTimeout(function() { controller.abort(); }, 20000);

  fetch('https://api.openai.com/v1/audio/transcriptions', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + apiKey },
    body: formData,
    signal: controller.signal
  }).then(function(res) {
    if (!res.ok) throw new Error('Whisper API returned ' + res.status);
    return res.json();
  }).then(function(data) {
    _vv._whisperInflight = false;
    clearTimeout(fetchTimeout);
    if (data.text && data.text.trim()) {
      _vvHandleTranscript(data.text.trim());
    }
    if (_vv.open && _vv.whisperMode && _vv.phase !== 'thinking' && _vv.phase !== 'speaking') {
      _vvWhisperRecord();
    }
  }).catch(function(err) {
    _vv._whisperInflight = false;
    clearTimeout(fetchTimeout);
    console.warn('[VoiceView] Whisper transcription error:', err.message);
    if (_vv.open && _vv.whisperMode) setTimeout(function() { _vvWhisperRecord(); }, 1000);
  });
}

// --- Transcript + command handling ---

// Reflect conversation-mode state into the voice-view controls and bind their
// change handlers (idempotent; safe to call on each open).
function _vvSyncConvoControls() {
  var toggle = document.getElementById('vvConvoToggle');
  var timeoutSel = document.getElementById('vvConvoTimeout');
  if (toggle) {
    toggle.checked = !!_vv.convoMode;
    if (!toggle._vvBound) {
      toggle._vvBound = true;
      toggle.addEventListener('change', function() {
        _vv.convoMode = !!toggle.checked;
        try { localStorage.setItem('vvConvoMode', _vv.convoMode ? '1' : '0'); } catch (e) {}
        // Apply immediately if currently idling between turns.
        if (_vv.open) {
          if (_vv.convoMode && _vv.phase === 'listening') {
            _vvEnterAwake(_vv.convoTimeoutMs);
          } else if (!_vv.convoMode && _vv.phase === 'awake') {
            if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
            _vvSetStatus('listening');
          }
        }
      });
    }
  }
  if (timeoutSel) {
    timeoutSel.value = String(_vv.convoTimeoutMs);
    if (!timeoutSel._vvBound) {
      timeoutSel._vvBound = true;
      timeoutSel.addEventListener('change', function() {
        var ms = parseInt(timeoutSel.value, 10);
        if (Number.isInteger(ms) && ms >= 5000 && ms <= 300000) {
          _vv.convoTimeoutMs = ms;
          try { localStorage.setItem('vvConvoTimeoutMs', String(ms)); } catch (e) {}
          if (_vv.open && _vv.phase === 'awake') _vvEnterAwake(ms);
        }
      });
    }
  }
}

// Enter the 'awake' conversation window. While awake, the user can speak follow
// ups without repeating the wake word. After timeoutMs of no speech we fall back
// to 'listening' (standby), which requires saying "Eva" again.
function _vvEnterAwake(timeoutMs) {
  _vvSetStatus('awake');
  if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
  _vv.awakeTimer = setTimeout(function() {
    _vv.awakeTimer = null;
    if (_vv.phase === 'awake') _vvSetStatus('listening');
  }, timeoutMs || 10000);
}

// Called when a turn completes. In conversation mode Eva stays awake for a
// follow-up; otherwise she returns to standby and waits for the wake word.
function _vvAfterTurn() {
  _vv._thinkingStart = null;
  if (!_vv.open) return;
  if (!(_vv.recognition || _vv.whisperMode)) { _vvSetStatus('idle'); return; }
  if (_vv.convoMode) {
    _vvEnterAwake(_vv.convoTimeoutMs);
  } else {
    _vvSetStatus('listening');
  }
  if (_vv.whisperMode) _vvWhisperRecord();
}

// --- Barge-in (interrupt Eva while she speaks) ---

// Hard-stop any TTS playback. Pausing the audio element silences the network
// voices; cancel() stops the browser SpeechSynthesis engine.
function _vvStopTTS() {
  // Cancel any in-flight chunked playback so queued sentences do not resume.
  if (typeof _ttsChunk !== 'undefined') { _ttsChunk.cancelled = true; _ttsChunk.active = false; }
  var audio = document.getElementById('audioPlayback');
  if (audio) {
    try { audio.pause(); } catch (e) {}
    try { audio.currentTime = 0; } catch (e) {}
  }
  if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (e) {} }
}

// Invoked when the barge monitor detects the user talking over Eva. Routes the
// speaking phase through its finalizer with the barged flag so Eva goes quiet
// and immediately opens a conversation window to catch the redirect.
function _vvBargeIn() {
  if (_vv.phase !== 'speaking') return;
  if (typeof _vv._finishSpeaking === 'function') {
    _vv._finishSpeaking(true);
  } else {
    // Fallback: finishSpeaking not set (stale state). Force recovery.
    _vvStopTTS();
    _vvStopBargeMonitor();
    _vvAfterBarge();
  }
}

// After a barge-in, open the conversation window (no wake word needed) and arm
// capture so the user's redirect is heard right away.
function _vvAfterBarge() {
  if (!_vv.open) return;
  if (!(_vv.recognition || _vv.whisperMode)) { _vvSetStatus('idle'); return; }
  // Re-arm speech recognition in case the browser killed it during TTS
  if (_vv.recognition) {
    try { _vv.recognition.stop(); } catch (_) {}
    setTimeout(function() {
      if (_vv.open) {
        try { _vv.recognition.start(); } catch (_) {}
      }
    }, 200);
  }
  _vvEnterAwake(_vv.convoTimeoutMs);
  if (_vv.whisperMode) _vvWhisperRecord();
}

// Watch the mic during the speaking phase. With echo cancellation removing
// Eva's own voice, sustained mic energy that also clears the current playback
// level means the user is talking over her, so we trigger a barge-in. A short
// startup grace avoids self-triggering on Eva's first syllable.
function _vvStartBargeMonitor() {
  _vvStopBargeMonitor();
  if (!_vv.analyser || !_vv.dataArray) return;
  var aboveSince = 0;
  var NEED_MS = 220;   // sustained user speech before interrupting
  var THRESH = 22;     // min average mic energy (0-255) to consider speech
  var graceUntil = performance.now() + 600;
  function loop() {
    if (_vv.phase !== 'speaking' || !_vv.open || !_vv.analyser) { _vv.bargeRAF = null; return; }
    _vv.analyser.getByteFrequencyData(_vv.dataArray);
    var sum = 0;
    for (var i = 0; i < _vv.dataArray.length; i++) sum += _vv.dataArray[i];
    var micAvg = sum / _vv.dataArray.length;

    // Current TTS playback level. With echo cancellation already removing Eva's
    // own voice from the mic, this only needs to be a light guard against any
    // residual echo, so the bar to interrupt stays low enough for a normal
    // speaking voice picked up at a distance.
    var ttsAvg = 0;
    if (_vv.ttsAnalyser && _vv.ttsDataArray) {
      _vv.ttsAnalyser.getByteFrequencyData(_vv.ttsDataArray);
      var ts = 0;
      for (var j = 0; j < _vv.ttsDataArray.length; j++) ts += _vv.ttsDataArray[j];
      ttsAvg = ts / _vv.ttsDataArray.length;
    }

    var now = performance.now();
    var isUser = now > graceUntil && micAvg > THRESH && micAvg > (ttsAvg * 0.35 + 5);
    if (isUser) {
      if (!aboveSince) aboveSince = now;
      else if (now - aboveSince >= NEED_MS) { _vv.bargeRAF = null; _vvBargeIn(); return; }
    } else {
      aboveSince = 0;
    }
    _vv.bargeRAF = requestAnimationFrame(loop);
  }
  _vv.bargeRAF = requestAnimationFrame(loop);
}

function _vvStopBargeMonitor() {
  if (_vv.bargeRAF) { cancelAnimationFrame(_vv.bargeRAF); _vv.bargeRAF = null; }
}

function _vvHandleTranscript(transcript) {
  // A response is mid-flight: if stuck too long, recover instead of ignoring forever.
  if (_vv.phase === 'thinking') {
    // Allow up to 90s of thinking, then force recovery
    if (!_vv._thinkingStart) _vv._thinkingStart = Date.now();
    if (Date.now() - _vv._thinkingStart > 90000) {
      console.warn('[VoiceView] Stuck in thinking for 90s, forcing recovery');
      _vv._thinkingStart = null;
      _vv.phase = 'listening';
      _vvSetStatus('listening');
      // Fall through to process the transcript instead of returning
    } else {
      return;
    }
  }
  // Spoken interruption while Eva is talking: barge in, then process the phrase
  // as the redirect. (Whisper mode does not record during speaking, so this
  // branch is reached only by the Web Speech recognizer; the energy monitor
  // covers whisper.)
  if (_vv.phase === 'speaking') {
    if (!transcript || transcript.trim().length <= 1) return;
    _vvBargeIn();
  }

  // Show transcript in HUD
  var transcriptEl = document.getElementById('vvTranscript');
  if (transcriptEl) transcriptEl.textContent = transcript;

  var lower = transcript.toLowerCase();
  var evaIdx = lower.indexOf('eva');

  if (evaIdx >= 0) {
    var command = transcript.substring(evaIdx + 3).trim().replace(/^[,.\s]+/, '').trim();
    if (command.length > 1) {
      _vvSendCommand(command, true);
    } else {
      // Wake word only: open the conversation window and wait for the command.
      _vvEnterAwake(_vv.convoMode ? _vv.convoTimeoutMs : 10000);
    }
    return;
  }

  if (_vv.phase === 'awake') {
    if (transcript.length > 1) {
      _vvSendCommand(transcript);
    } else {
      // Too short to act on; stay awake and re-arm the standby timer instead of
      // dropping out of the conversation on a stray noise.
      _vvEnterAwake(_vv.convoMode ? _vv.convoTimeoutMs : 10000);
    }
  }
}

// Short, varied acknowledgment phrases spoken instantly when a voice command is
// received, before the (slower) real reply is generated and synthesized.
var _VV_ACK_PHRASES = [
  'On it.', 'One moment.', 'Let me take a look.', 'Working on it.',
  'Sure, give me a second.', 'Okay, looking into that.', 'Got it.', 'Right away.'
];

// Speak an instant local acknowledgment via the browser speech synth (offline,
// near-zero latency) regardless of the configured reply TTS engine, so there is
// immediate audible feedback while the cognition pipeline runs. The `_ackActive`
// flag tells the voice-view speech monitor to ignore this filler so it does not
// count as the real reply starting.
function _vvSpeakAck() {
  try {
    if (typeof window.speechSynthesis === 'undefined' ||
        typeof window.SpeechSynthesisUtterance === 'undefined') return;
    var phrase = _VV_ACK_PHRASES[Math.floor(Math.random() * _VV_ACK_PHRASES.length)];
    _vv._ackActive = true;
    if (_vv._ackTimer) { clearTimeout(_vv._ackTimer); _vv._ackTimer = null; }
    var u = new SpeechSynthesisUtterance(phrase);
    u.lang = 'en-US';
    u.rate = 1.05;
    u.pitch = 1.0;
    u.volume = 0.9;
    u.onend = function () { _vv._ackActive = false; };
    u.onerror = function () { _vv._ackActive = false; };
    try { window.speechSynthesis.cancel(); } catch (_) {}
    window.speechSynthesis.speak(u);
    // Safety: clear the guard even if onend never fires (some engines drop it).
    _vv._ackTimer = setTimeout(function () { _vv._ackActive = false; }, 4000);
  } catch (e) {
    _vv._ackActive = false;
  }
}

function _vvSendCommand(command, fromWakeWord) {
  // Natural agent confirmation via voice: if an agent is parked on a yes/no,
  // interpret this utterance as the answer instead of a new command.
  // But NOT if the user used the wake word "Eva" — that signals a new intent.
  if (!fromWakeWord && typeof _agentConfirm !== 'undefined' && _agentConfirm.pending) {
    if (_maybeAnswerAgentConfirm(command)) {
      var transcriptElC = document.getElementById('vvTranscript');
      if (transcriptElC) transcriptElC.textContent = '\u25B8 ' + command;
      // Stay in conversation: return to listening/awake after answering.
      if (typeof _vvAfterTurn === 'function') _vvAfterTurn();
      return;
    }
  }
  _vv.lastTranscript = command;
  _vv.cmdStart = performance.now();
  _vv._thinkingStart = Date.now();
  if (_vv.awakeTimer) { clearTimeout(_vv.awakeTimer); _vv.awakeTimer = null; }
  _vvSetStatus('thinking');
  _vvHideAssets();
  // Speak an instant local acknowledgment to cover pipeline + TTS latency, so
  // the turn does not feel like dead air before the real reply is synthesized.
  _vvSpeakAck();

  // Show command in transcript area
  var transcriptEl = document.getElementById('vvTranscript');
  if (transcriptEl) transcriptEl.textContent = '\u25B8 ' + command;

  var txtMsg = document.getElementById('txtMsg');
  if (txtMsg) txtMsg.textContent = command;

  var autoSpeak = document.getElementById('autoSpeak');
  _vv._wasAutoSpeak = autoSpeak ? autoSpeak.checked : false;
  if (autoSpeak) autoSpeak.checked = true;

  _vvWatchForResponse();

  if (typeof sendData === 'function') sendData();
}

function _vvWatchForResponse() {
  var txtOutput = document.getElementById('txtOutput');
  if (!txtOutput) return;

  if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
  _vvDetachSpeakStartListeners();
  if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
  if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }

  var finished = false;   // the whole turn has resolved
  var speaking = false;   // real audio/synth playback has started
  var gotText = false;    // Eva's text response has been observed
  var audio = document.getElementById('audioPlayback');
  var synth = window.speechSynthesis;

  function cleanupTriggers() {
    if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
    _vvDetachSpeakStartListeners();
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }
  }

  function finishToListening() {
    if (finished || !_vv.open) return;
    finished = true;
    cleanupTriggers();
    _vvRestoreAutoSpeak();
    if (_vv.open) {
      _vvSetStatus('listening');
      if (_vv.whisperMode) _vvWhisperRecord();
    }
  }

  // Enter the speaking phase ONLY when real audio is heard. The earlier design
  // flipped to 'speaking' as soon as Eva's text appeared, but TTS (network
  // voices, or a slow cognition turn that renders text well before audio) can
  // lag the text by seconds. That produced a green 'speaking' orb with no
  // sound, which then timed out back to 'listening' just before the real audio
  // started. Triggering on the actual audio/synth start keeps them in sync.
  function beginSpeaking() {
    if (finished || speaking || !_vv.open) return;
    speaking = true;
    if (_vv.speakObserver) { _vv.speakObserver.disconnect(); _vv.speakObserver = null; }
    _vvDetachSpeakStartListeners();
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    if (_vv._postTextTimer) { clearTimeout(_vv._postTextTimer); _vv._postTextTimer = null; }

    var evaResponse = (typeof lastResponse === 'string') ? lastResponse.trim() : '';
    if (evaResponse) _vv.lastEvaReply = evaResponse;

    _vvSetStatus('speaking');
    _vvConnectTTSAnalyser();
    _vvStartBargeMonitor();

    // Single finalizer for the speaking phase, reachable from both the natural
    // speech-end and a user barge-in. Idempotent so whichever path wins runs the
    // teardown exactly once.
    var ended = false;
    function finishSpeaking(barged) {
      if (ended) return;
      ended = true;
      _vv._finishSpeaking = null;
      _vvStopBargeMonitor();
      if (barged) _vvStopTTS();
      _vvDisconnectTTSAnalyser();
      finished = true;
      cleanupTriggers();
      _vvRestoreAutoSpeak();
      if (barged) _vvAfterBarge(); else _vvAfterTurn();
    }
    _vv._finishSpeaking = finishSpeaking;

    _vvWaitForSpeechEnd(function() { finishSpeaking(false); });
  }

  // Eva's text response arrived. Stay in 'thinking' and wait for audio to begin
  // (the reliable speaking trigger). If no audio starts within the grace window
  // the response was effectively silent, so return to listening rather than
  // showing a speaking phase that never produces sound.
  function onResponseText() {
    if (finished || speaking || gotText || !_vv.open) return;
    var evaResponse = (typeof lastResponse === 'string') ? lastResponse.trim() : '';
    if (!evaResponse || evaResponse === _vv.lastEvaReply) return;
    gotText = true;
    _vv.lastEvaReply = evaResponse;
    if (_vv._watchTimer) { clearTimeout(_vv._watchTimer); _vv._watchTimer = null; }
    _vv._postTextTimer = setTimeout(function() {
      _vv._postTextTimer = null;
      if (!speaking && !finished) {
        // Text completed but no audio played (silent or disabled TTS). The turn
        // is still done, so continue the conversation rather than abandoning it.
        finished = true;
        cleanupTriggers();
        _vvRestoreAutoSpeak();
        _vvAfterTurn();
      }
    }, 20000);
  }

  _vv.speakObserver = new MutationObserver(onResponseText);
  _vv.speakObserver.observe(txtOutput, { childList: true, subtree: true, characterData: true });

  // Audio playback / synth start are the authoritative speaking triggers.
  _vv._onSpeakStart = function() { beginSpeaking(); };
  if (audio) {
    audio.addEventListener('playing', _vv._onSpeakStart);
    audio.addEventListener('play', _vv._onSpeakStart);
  }
  if (synth) {
    _vv._synthPoll = setInterval(function() {
      if (finished || !_vv.open) { clearInterval(_vv._synthPoll); _vv._synthPoll = null; return; }
      // Ignore the short acknowledgment filler so it does not flip the phase
      // to 'speaking' before the real reply audio actually starts.
      if (synth.speaking && !_vv._ackActive) beginSpeaking();
    }, 200);
  }

  // No-response watchdog. Heavy cognition turns (draft + review on slow models)
  // can legitimately run for minutes, so while Eva is still 'thinking' and no
  // text or audio has arrived we keep waiting and re-arm. Once text arrives the
  // post-text grace above takes over; once audio starts beginSpeaking does. We
  // only force a recovery here if the phase already moved on or a hard ceiling
  // is hit (a stuck turn that never produced text or audio).
  var watchStart = performance.now();
  var WATCH_ABS_MAX = 600000; // 10 min hard ceiling
  function watchdog() {
    _vv._watchTimer = null;
    if (finished || speaking || gotText || !_vv.open) return;
    if (_vv.phase === 'thinking' && (performance.now() - watchStart) < WATCH_ABS_MAX) {
      _vv._watchTimer = setTimeout(watchdog, 10000);
      return;
    }
    finishToListening();
  }
  _vv._watchTimer = setTimeout(watchdog, 60000);
}

function _vvDetachSpeakStartListeners() {
  var audio = document.getElementById('audioPlayback');
  if (audio && _vv._onSpeakStart) {
    try { audio.removeEventListener('playing', _vv._onSpeakStart); } catch(e) {}
    try { audio.removeEventListener('play', _vv._onSpeakStart); } catch(e) {}
  }
  _vv._onSpeakStart = null;
  if (_vv._synthPoll) { clearInterval(_vv._synthPoll); _vv._synthPoll = null; }
}

function _vvRestoreAutoSpeak() {
  if (_vv._wasAutoSpeak === undefined) return;
  var autoSpeak = document.getElementById('autoSpeak');
  if (autoSpeak) autoSpeak.checked = _vv._wasAutoSpeak;
  delete _vv._wasAutoSpeak;
}

function _vvWaitForSpeechEnd(callback) {
  var audio = document.getElementById('audioPlayback');

  // Chunked TTS fires the audio element's 'ended' between sentence chunks, so
  // wait for the whole chunk queue to drain rather than a single 'ended'.
  if (typeof _ttsChunk !== 'undefined' && _ttsChunk.active) {
    var chunkPoll = setInterval(function () {
      if (!_vv.open || !_ttsChunk.active || _ttsChunk.cancelled) {
        clearInterval(chunkPoll); setTimeout(callback, 300);
      }
    }, 300);
    setTimeout(function () { clearInterval(chunkPoll); callback(); }, 600000);
    return;
  }

  var synth = window.speechSynthesis;
  if (synth && synth.speaking) {
    var synthCheck = setInterval(function() {
      if (!synth.speaking) { clearInterval(synthCheck); setTimeout(callback, 500); }
    }, 500);
    setTimeout(function() { clearInterval(synthCheck); callback(); }, 30000);
    return;
  }

  if (!audio) { setTimeout(callback, 2000); return; }

  var checkCount = 0;
  var maxChecks = 30;
  function check() {
    if (!_vv.open) { callback(); return; }
    checkCount++;
    if (checkCount > maxChecks) { callback(); return; }
    if (!audio.paused && !audio.ended) {
      audio.addEventListener('ended', function onEnd() {
        audio.removeEventListener('ended', onEnd);
        setTimeout(callback, 500);
      }, { once: true });
    } else {
      setTimeout(check, 1000);
    }
  }
  setTimeout(check, 1500);
}

function OnLoad() {
    // Initialize session manager (restores active session if any)
    if (typeof initSessions === 'function') initSessions();

    // Only show the welcome message if no session was restored
    var txtOutput = document.getElementById("txtOutput");
    if (!txtOutput.innerHTML.trim()) {
      showWelcome();
    }
}

// ── Eva Theme helpers ──────────────────────────────────────
// Click a suggestion bubble → populate input and send
function evaSuggestionClick(btn) {
  var prompt = btn.getAttribute('data-prompt');
  var input = document.getElementById('txtMsg');
  if (input && prompt) {
    input.textContent = prompt;
    sendData();
  }
}

// Hide the Eva welcome MOTD when user sends first message
function hideEvaWelcome() {
  var w = document.getElementById('evaWelcome');
  if (w) w.style.display = 'none';
}

// Populate Eva sidebar's recent sessions (Today section)
function populateEvaSidebarSessions() {
  var ul = document.getElementById('evaSidebarSessionList');
  if (!ul) return;
  ul.innerHTML = '';
  if (typeof getAllSessions !== 'function') return;
  getAllSessions().then(function(sessions) {
    var today = new Date().toDateString();
    var recent = (sessions || [])
      .filter(function(s) { return new Date(s.updatedAt || s.createdAt).toDateString() === today; })
      .sort(function(a, b) { return (b.updatedAt || b.createdAt) - (a.updatedAt || a.createdAt); })
      .slice(0, 5);
    if (!recent.length) {
      ul.innerHTML = '<li class="eva-session-empty">No chats yet today</li>';
      return;
    }
    recent.forEach(function(s) {
      var li = document.createElement('li');
      li.className = 'eva-session-item';
      li.textContent = s.title || 'Untitled';
      li.title = s.title || 'Untitled';
      li.onclick = function() { if (typeof loadSession === 'function') loadSession(s.id); };
      ul.appendChild(li);
    });
  }).catch(function() {});
}

// Apply UI theme (default | lcars | eva | eva-*)
function applyTheme(theme) {
  const body = document.body;
  if (!body) return;

  // Remove all theme-* classes (covers variants like theme-eva-rose, etc.)
  body.className = body.className.replace(/\btheme-\S+/g, '').trim();
  // eva.css holds global component styles (voice view, welcome screen, color
  // variants) that all themes rely on, not just theme-eva. Keep it loaded for
  // every theme so switching to LCARS/legacy doesn't strip those base rules
  // (which previously left the voice-view HUD visible and broke the layout).
  ensureThemeStylesheet('eva', 'core/themes/eva.css');
  unloadThemeStylesheet('lcars');

  // Add selected theme class
  var isEva = (theme === 'eva' || theme.indexOf('eva-') === 0);
  if (isEva) {
    body.classList.add('theme-eva');
    if (theme !== 'eva') body.classList.add('theme-' + theme);
    ensureThemeStylesheet('eva', 'core/themes/eva.css');
    // Move speak button into sidebar (same layout as LCARS)
    const lcarsChipSand = document.getElementById('lcarsChipSand');
    const speakBtn = document.getElementById('speakSend');
    if (lcarsChipSand && speakBtn && !lcarsChipSand.contains(speakBtn)) {
      lcarsChipSand.appendChild(speakBtn);
      speakBtn.title = 'Speak';
      speakBtn.textContent = 'Speak';
    }
    // Move Print button into sidebar
    const lcarsChipPrint = document.getElementById('lcarsChipPrint');
    const printBtn = document.getElementById('printButton');
    if (lcarsChipPrint && printBtn && !lcarsChipPrint.contains(printBtn)) {
      lcarsChipPrint.appendChild(printBtn);
      printBtn.title = 'Print Output';
    }
  } else if (theme === 'lcars') {
    body.classList.add('theme-lcars');
  // Ensure LCARS stylesheet is present (modular theme loader)
  ensureThemeStylesheet('lcars', 'core/themes/lcars.css');
    // Move speak button into sidebar
    const lcarsChipSand = document.getElementById('lcarsChipSand');
    const speakBtn = document.getElementById('speakSend');
    if (lcarsChipSand && speakBtn && !lcarsChipSand.contains(speakBtn)) {
      lcarsChipSand.appendChild(speakBtn);
      speakBtn.title = 'Speak';
      speakBtn.textContent = 'Speak';
    }
    // Move Print button beneath Speak in sidebar
    const lcarsChipPrint = document.getElementById('lcarsChipPrint');
    const printBtn = document.getElementById('printButton');
    if (lcarsChipPrint && printBtn && !lcarsChipPrint.contains(printBtn)) {
      lcarsChipPrint.appendChild(printBtn);
      printBtn.title = 'Print Output';
    }
  } else {
    // Restore speak button to its original container when leaving LCARS
    const container = document.querySelector('.container');
    const speakBtn = document.getElementById('speakSend');
    if (container && speakBtn && !container.contains(speakBtn)) {
      container.appendChild(speakBtn);
    }
    // Restore Print button to footer when leaving LCARS
    const footer = document.querySelector('footer');
    const printBtn = document.getElementById('printButton');
    if (footer && printBtn && !footer.contains(printBtn)) {
      footer.appendChild(printBtn);
    }
  }

  // Persist
  try { localStorage.setItem('theme', theme); } catch (e) {}

  // Update available model options according to theme
  updateModelOptionsForTheme(theme);

  // Ensure monitors dock is visible on LCARS and Eva themes
  var mon = document.getElementById('lcarsMonitorsDock');
  if (mon) mon.style.display = (theme === 'lcars') ? 'block' : 'none';

  // Toggle Eva sidebar visibility
  var evaSidebar = document.getElementById('evaSidebar');
  if (evaSidebar) evaSidebar.style.display = isEva ? 'flex' : 'none';

  // Toggle Eva disclaimer
  var evaDisclaimer = document.getElementById('evaDisclaimer');
  if (evaDisclaimer) evaDisclaimer.style.display = isEva ? 'block' : 'none';

  // Eva themes all use the thumb-125 portrait
  var welcomeAvatar = document.querySelector('.eva-welcome-avatar');
  var sidebarAvatar = document.querySelector('.eva-sidebar-avatar');
  if (isEva) {
    if (welcomeAvatar) welcomeAvatar.src = 'core/img/thumb-125.jpeg';
    if (sidebarAvatar) sidebarAvatar.src = 'core/img/thumb-125.jpeg';
  }

  // Populate Eva sidebar sessions
  if (isEva) populateEvaSidebarSessions();
}

// Modular theme stylesheet loader (extensible for future themes)
function ensureThemeStylesheet(themeName, href) {
  const id = `theme-${themeName}-css`;
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.id = id;
  link.href = href;
  document.head.appendChild(link);
}

function unloadThemeStylesheet(themeName) {
  const id = `theme-${themeName}-css`;
  const el = document.getElementById(id);
  if (el && el.parentNode) el.parentNode.removeChild(el);
}


// Track user intent for image handling
var _lastUserAskedGenerate = false;
var _lastUserImageSubject = '';  // extracted from user's message before send
var _lastUserAskedImage = false; // true if user asked for any image (generate, show, find)

/**
 * Extract image subject from the user's own message.
 * "show me an image of a cat" → "cat"
 * "generate a picture of a sunset over mountains" → "sunset over mountains"
 */
function _extractUserImageSubject(text) {
  if (!text) return '';
  // Match patterns like "image of X", "picture of X", "photo of X"
  var m = text.match(/(?:image|picture|photo|illustration|drawing|painting)\s+(?:of\s+)?(?:an?\s+)?(.+)/i);
  if (m) return m[1].replace(/[?.!]+$/, '').trim();
  // Match "show me X", "generate X"
  m = text.match(/(?:show\s+me|generate|create|draw|make|display)\s+(?:an?\s+)?(?:image\s+)?(?:of\s+)?(?:an?\s+)?(.+)/i);
  if (m) return m[1].replace(/[?.!]+$/, '').trim();
  return '';
}

// Detect image generation intent from user input (called before every send)
function _detectGenerationIntent() {
  var txtMsg = document.getElementById('txtMsg');
  if (txtMsg) {
    var userText = txtMsg.innerText || txtMsg.textContent || '';
    _lastUserAskedGenerate = _isGenerationRequest(userText);
    _lastUserImageSubject = _extractUserImageSubject(userText);
    _lastUserAskedImage = _isImageRequest(userText);
  }
}

function updateButton() {
  applyStandaloneSimplifications();
    var btnSend = document.getElementById("btnSend");
  if (btnSend) btnSend.onclick = sendData;
}

function sendData() {
    // Natural agent confirmation: if the browser/desktop agent is parked waiting
    // on a yes/no (e.g. the final purchase), interpret this message as the answer
    // and route it to the agent instead of sending a normal chat turn.
    if (typeof _agentConfirm !== 'undefined' && _agentConfirm.pending) {
      var _txtMsgEl = document.getElementById('txtMsg');
      var _pendingText = _txtMsgEl ? (_txtMsgEl.innerText || _txtMsgEl.textContent || '') : '';
      if (_maybeAnswerAgentConfirm(_pendingText)) {
        if (_txtMsgEl) _txtMsgEl.innerHTML = '';
        return;
      }
    }
    // Hide Eva welcome MOTD on first send
    hideEvaWelcome();
  applyStandaloneSimplifications();

    // Logic required for initial message
    var selModel = document.getElementById("selModel");
    var _dispatchEnvelope = null;
    if (typeof newEnvelopeTurn === 'function') {
      newEnvelopeTurn();
      var _turnEnvelope = captureRequestEnvelope();
      _dispatchEnvelope = _turnEnvelope;
      window._evaSessionId = _turnEnvelope.session_id;
      window._evaTurnId = _turnEnvelope.turn_id;
    }
    var _egressMode = (typeof window !== 'undefined' && window.evaStandalone && window.evaStandalone.egressMode) || 'cloud';
    if (_egressMode !== 'cloud' && selModel.value !== 'aig' && selModel.value !== 'lm-studio') {
      setStatus('error', 'Cloud models are disabled in ' + _egressMode + ' mode. Select Eva (AIG) or LM Studio.');
      return;
    }

  // Detect if user wants image generation (for renderEvaResponse routing)
  _detectGenerationIntent();

  if (selModel.value === 'aig') {
        clearText();
      aigSend(null, _dispatchEnvelope);
    } else if (selModel.value.indexOf('copilot-') === 0) {
        clearText();
        copilotSend(_dispatchEnvelope);
    } else if (selModel.value == "gpt-4o-mini" || selModel.value == "o1" || selModel.value == "o1-mini" || selModel.value == "gpt-4o" || selModel.value == "o3-mini" || selModel.value == "o1-preview" || selModel.value == "gpt-5-mini" || selModel.value == "latest") {
        clearText();
        trboSend(_dispatchEnvelope);
    } else if (selModel.value == "gemini") {
        clearText();
        geminiSend(_dispatchEnvelope);
    } else if (selModel.value == "lm-studio") {
        clearText();
      if (_egressMode !== 'cloud' && typeof isEvaStandalone === 'function' && isEvaStandalone()) {
        aigSend('lmstudio', _dispatchEnvelope);
      } else {
        lmsSend(_dispatchEnvelope);
      }
    } else if (selModel.value == "dall-e-3") {
        clearText();
        dalle3Send();
    } else {
        clearText();
        // Send();
        document.getElementById("txtOutput").innerHTML = "\n" + "Invalid Model"
        console.error('Invalid Model')
    }
}

// Footer status helper
function setStatus(type, text) {
  var el = document.getElementById('idText');
  if (el) {
    el.classList.remove('status-info','status-warn','status-error');
    if (type === 'warn') el.classList.add('status-warn');
    else if (type === 'error') el.classList.add('status-error');
    else el.classList.add('status-info');
    if (text) el.textContent = text;
  }
  // Mirror into the Eva-theme footer status line so users on the Eva theme
  // (which hides the LCARS monitor dock) still see model/route updates.
  var foot = document.getElementById('evaStatusFooter');
  if (foot) {
    foot.classList.remove('status-info','status-warn','status-error');
    if (type === 'warn') foot.classList.add('status-warn');
    else if (type === 'error') foot.classList.add('status-error');
    else foot.classList.add('status-info');
    var msg = text || '';
    foot.textContent = msg;
    foot.setAttribute('data-empty', msg ? 'false' : 'true');
  }
}

// --- Cognitive layer settings (eva / reviewer) ---
function _cogPopulateModelSelect(targetId) {
  var src = document.getElementById('selAIGBackend');
  var dst = document.getElementById(targetId);
  if (!src || !dst) return;
  // Clone the option/optgroup tree from the AIG backend selector so the
  // cognition selectors always stay in sync with the live model catalog.
  dst.innerHTML = '';
  Array.from(src.children).forEach(function (child) {
    dst.appendChild(child.cloneNode(true));
  });
}

var COG_PROMPT_FIELDS = {
  eva: { id: 'cogEvaPrompt', key: 'cogEvaPrompt', cfgKey: 'evaPrompt' },
  reviewer: { id: 'cogReviewerPrompt', key: 'cogReviewerPrompt', cfgKey: 'reviewerPrompt' }
};

function _cogPromptDefault(role) {
  var defaults = (window.EvaCognition && window.EvaCognition.DEFAULT_PROMPTS) ||
    ((typeof Cognition !== 'undefined' && Cognition.DEFAULT_PROMPTS) ? Cognition.DEFAULT_PROMPTS : {});
  return defaults[role] || '';
}

function _cogStoredPromptOrDefault(role) {
  var field = COG_PROMPT_FIELDS[role];
  if (!field) return '';
  try {
    var stored = localStorage.getItem(field.key);
    if (stored) return stored;
  } catch (_) {}
  return _cogPromptDefault(role);
}

function cogInit() {
  if (typeof Cognition === 'undefined') return;
  ['cogEvaModel', 'cogReviewerModel']
    .forEach(_cogPopulateModelSelect);
  var cfg = Cognition.getCfg();
  var $ = function (id) { return document.getElementById(id); };
  if ($('cogEnabled'))           $('cogEnabled').checked          = !!cfg.enabled;
  if ($('cogShowTrace'))         $('cogShowTrace').checked        = !!cfg.showTrace;
  if ($('cogEvaModel'))          $('cogEvaModel').value           = cfg.evaModel;
  if ($('cogReviewerModel'))     $('cogReviewerModel').value      = cfg.reviewerModel;
  if ($('cogMaxCycles'))         $('cogMaxCycles').value          = String(cfg.maxCycles);
  if ($('cogEvaPrompt'))         $('cogEvaPrompt').value         = _cogStoredPromptOrDefault('eva');
  if ($('cogReviewerPrompt'))    $('cogReviewerPrompt').value    = _cogStoredPromptOrDefault('reviewer');
  cogUpdateBadge();
}

function cogPersist() {
  if (typeof Cognition === 'undefined') return;
  var $ = function (id) { return document.getElementById(id); };
  var partial = {
    enabled:           $('cogEnabled')          ? $('cogEnabled').checked        : false,
    showTrace:         $('cogShowTrace')        ? $('cogShowTrace').checked      : false,
    evaModel:          $('cogEvaModel')         ? $('cogEvaModel').value         : '',
    reviewerModel:     $('cogReviewerModel')    ? $('cogReviewerModel').value    : '',
    maxCycles:         $('cogMaxCycles')        ? $('cogMaxCycles').value        : '1'
  };
  Object.keys(COG_PROMPT_FIELDS).forEach(function (role) {
    var field = COG_PROMPT_FIELDS[role];
    var el = $(field.id);
    if (!el) return;
    if (el.value === _cogPromptDefault(role)) {
      try { localStorage.removeItem(field.key); } catch (_) {}
      return;
    }
    partial[field.cfgKey] = el.value;
  });
  Cognition.setCfg(partial);
  cogUpdateBadge();
  cogUpdatePromptsTabUI();
}

function cogUpdatePromptsTabUI() {
  var $ = function (id) { return document.getElementById(id); };
  var enabled = false;
  try { enabled = (typeof Cognition !== 'undefined' && Cognition.isEnabled && Cognition.isEnabled()); } catch (_) {}
  
  // Show/hide cognitive layer sections in Prompts tab
  var indicator = $('cogStatusIndicator');
  var promptsSection = $('cogPromptsSection');
  
  if (indicator) {
    indicator.style.display = enabled ? 'flex' : 'none';
  }
  if (promptsSection) {
    promptsSection.style.display = enabled ? 'block' : 'none';
  }
}

function cogUpdateBadge() {
  var badge = document.getElementById('cogBadge');
  if (!badge) return;
  var on = false;
  try { on = (typeof Cognition !== 'undefined' && Cognition.isEnabled && Cognition.isEnabled()); } catch (_) {}
  badge.setAttribute('data-active', on ? 'true' : 'false');
  badge.textContent = on ? 'Cognition: on' : 'Cognition: off';
}

function _cogApplyDefaultPrompt(role) {
  var field = COG_PROMPT_FIELDS[role];
  if (!field) return false;
  try { localStorage.removeItem(field.key); } catch (_) {}
  var el = document.getElementById(field.id);
  if (!el) return false;
  el.value = _cogPromptDefault(role);
  return true;
}

function _cogNotifyPromptChange(role) {
  var field = COG_PROMPT_FIELDS[role];
  var el = field ? document.getElementById(field.id) : null;
  if (typeof cogPersist === 'function') {
    cogPersist();
  } else if (el) {
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}

function cogResetPrompt(role) {
  if (_cogApplyDefaultPrompt(role)) _cogNotifyPromptChange(role);
}

// --- Monitors: Token, Network, Session ---

// Better token estimation: ~3.5 chars per token for English, account for whitespace/punctuation
function estimateTokensFromText(str) {
  if (!str) return 0;
  var s = String(str);
  // Count words (roughly 1.3 tokens per word on average)
  var words = s.split(/\s+/).filter(function(w) { return w.length > 0; }).length;
  // Count special chars/punctuation as extra tokens
  var specials = (s.match(/[^a-zA-Z0-9\s]/g) || []).length;
  return Math.ceil(words * 1.3 + specials * 0.5);
}

// Map of model -> context window size
const MODEL_CONTEXT_WINDOWS = {
  'gpt-4o': 128000,
  'gpt-4o-mini': 128000,
  'o1': 200000,
  'o1-mini': 200000,
  'o1-preview': 200000,
  'o3-mini': 200000,
  'gpt-5-mini': 200000,
  'latest': 200000,
  'copilot-gpt-4o': 128000,
  'copilot-gpt-4o-mini': 128000,
  'copilot-o3-mini': 200000,
  'copilot-gpt-4.1': 1048576,
  'copilot-gpt-5': 200000,
  'copilot-o4-mini': 200000,
  'copilot-deepseek-r1': 128000,
  'copilot-llama-4-maverick': 1000000,
  'copilot-acp': 128000,
  'aig': 200000,
  'gemini': 1000000,
  'lm-studio': 32768,
  'dall-e-3': 0
};

// Network monitoring state
var _netStats = { requests: 0, errors: 0, lastLatency: 0, lastStatus: '', lastProvider: '' };

// Intercept fetch to track network stats
var _apiHostnames = ['api.openai.com', 'models.inference.ai.azure.com', 'generativelanguage.googleapis.com'];

function _isAPICall(url) {
  if (typeof url !== 'string') return false;
  try {
    var parsed = new URL(url, window.location.origin);
    if (_apiHostnames.indexOf(parsed.hostname) >= 0) return true;
    if (parsed.hostname === 'localhost' && (parsed.port === '1234' || parsed.port === '8888')) return true;
    if (parsed.port === '8888') return true;
    return false;
  } catch (e) {
    return false;
  }
}

(function() {
  var origFetch = window.fetch;
  window.fetch = function() {
    var url = arguments[0];
    if (!_isAPICall(url)) return origFetch.apply(this, arguments);

    _netStats.requests++;
    var start = performance.now();
    _netStats.lastProvider = _detectProvider(url);

    return origFetch.apply(this, arguments).then(function(resp) {
      _netStats.lastLatency = Math.round(performance.now() - start);
      _netStats.lastStatus = resp.status + ' ' + (resp.ok ? 'OK' : resp.statusText);
      if (!resp.ok) _netStats.errors++;
      updateNetMonitor();
      return resp;
    }).catch(function(err) {
      _netStats.lastLatency = Math.round(performance.now() - start);
      _netStats.lastStatus = 'Error';
      _netStats.errors++;
      updateNetMonitor();
      throw err;
    });
  };
})();

function _detectProvider(url) {
  try {
    var parsed = new URL(url, window.location.origin);
    if (parsed.hostname === 'api.openai.com') return 'OpenAI';
    if (parsed.hostname === 'models.inference.ai.azure.com') return 'GitHub Models';
    if (parsed.hostname === 'generativelanguage.googleapis.com') return 'Gemini';
    if (parsed.hostname === 'localhost' && parsed.port === '1234') return 'lm-studio';
    if (parsed.port === '8888') return 'ACP Bridge';
  } catch (e) {}
  return 'Unknown';
}

function getSelectedModel() {
  const sel = document.getElementById('selModel');
  return sel ? sel.value : '';
}

// Count all conversation messages across all providers
function _countAllMessages() {
  var count = 0;
  ['messages', 'copilotMessages', 'copilotACPMessages', 'geminiMessages', 'openLLMessages', 'aigMessages'].forEach(function(key) {
    try {
      var raw = localStorage.getItem(key);
      if (raw) {
        var msgs = JSON.parse(raw);
        count += msgs.length;
      }
    } catch(e) {}
  });
  return count;
}

// Compute tokens from all active message stores
function computeMessagesTokens() {
  var model = getSelectedModel();
  var keys = ['messages']; // default OpenAI
  if (model === 'copilot-acp') keys = ['copilotACPMessages'];
  else if (model.indexOf('copilot-') === 0) keys = ['copilotMessages'];
  else if (model === 'gemini') keys = ['geminiMessages'];
  else if (model === 'lm-studio') keys = ['openLLMessages'];

  var acc = 0;
  keys.forEach(function(key) {
    try {
      var raw = localStorage.getItem(key);
      if (!raw) return;
      var msgs = JSON.parse(raw);
      msgs.forEach(function(m) {
        if (!m) return;
        if (typeof m.content === 'string') {
          acc += estimateTokensFromText(m.content);
        } else if (Array.isArray(m.content)) {
          m.content.forEach(function(part) {
            if (part.type === 'text' && part.text) acc += estimateTokensFromText(part.text);
            if (part.text) acc += estimateTokensFromText(part.text);
          });
        }
        // Gemini format (parts array)
        if (Array.isArray(m.parts)) {
          m.parts.forEach(function(part) {
            if (part.text) acc += estimateTokensFromText(part.text);
          });
        }
      });
    } catch(e) {}
  });
  return acc;
}

function computeLastResponseTokens() {
  try {
    var txtOut = document.getElementById('txtOutput');
    if (!txtOut) return 0;
    var bubbles = txtOut.querySelectorAll('.eva-bubble .md, .eva-bubble');
    if (bubbles && bubbles.length) {
      return estimateTokensFromText(bubbles[bubbles.length - 1].textContent || '');
    }
    return 0;
  } catch(e) { return 0; }
}

function updateTokenMonitor() {
  var model = getSelectedModel();
  var windowSize = MODEL_CONTEXT_WINDOWS[model] || 128000;
  var msgTokens = computeMessagesTokens();
  var respTokens = computeLastResponseTokens();
  var used = msgTokens + respTokens;
  var pct = windowSize > 0 ? Math.min(100, Math.round((used / windowSize) * 100)) : 0;

  var bar = document.getElementById('ctxFillBar');
  var text = document.getElementById('ctxFillText');
  var winText = document.getElementById('modelWindowText');
  var msgText = document.getElementById('messagesTokensText');
  var respText = document.getElementById('lastResponseTokensText');

  if (bar) {
    bar.style.width = pct + '%';
    // Color the bar based on fill level
    if (pct > 80) bar.style.background = 'linear-gradient(90deg, #ff6b6b, #ee5a24)';
    else if (pct > 50) bar.style.background = 'linear-gradient(90deg, #feca57, #ff9f43)';
    else bar.style.background = '';
  }
  if (text) text.textContent = pct + '% \u2014 ~' + used.toLocaleString() + ' / ' + windowSize.toLocaleString();

  // Show model name + window
  var modelName = model || 'none';
  var sel = document.getElementById('selModel');
  if (sel && sel.selectedOptions && sel.selectedOptions[0]) {
    modelName = sel.selectedOptions[0].text;
  }
  if (winText) winText.textContent = modelName + ' (' + (windowSize > 0 ? (windowSize / 1000) + 'k' : 'N/A') + ')';
  if (msgText) msgText.textContent = '~' + msgTokens.toLocaleString() + ' tokens';
  if (respText) respText.textContent = '~' + respTokens.toLocaleString() + ' tokens';
}

function updateNetMonitor() {
  var latEl = document.getElementById('netLatencyText');
  var statEl = document.getElementById('netStatusText');
  var reqEl = document.getElementById('netRequestCountText');
  var errEl = document.getElementById('netErrorCountText');

  if (latEl) {
    var lat = _netStats.lastLatency;
    latEl.textContent = lat > 0 ? (lat < 1000 ? lat + 'ms' : (lat / 1000).toFixed(1) + 's') + ' \u2014 ' + _netStats.lastProvider : '\u2014';
  }
  if (statEl) statEl.textContent = _netStats.lastStatus || '\u2014';
  if (reqEl) reqEl.textContent = _netStats.requests.toString();
  if (errEl) {
    errEl.textContent = _netStats.errors.toString();
    errEl.style.color = _netStats.errors > 0 ? '#ff6b6b' : '';
  }
}

function updateSessionMonitor() {
  var model = getSelectedModel();
  var provEl = document.getElementById('sessProviderText');
  var msgEl = document.getElementById('sessMsgCountText');
  var acpEl = document.getElementById('sessACPText');
  var mcpEl = document.getElementById('sessMCPText');

  // Provider
  if (provEl) {
    if (model.indexOf('copilot-') === 0) provEl.textContent = model === 'copilot-acp' ? 'Copilot ACP' : 'GitHub Models';
    else if (model === 'gemini') provEl.textContent = 'Google Gemini';
    else if (model === 'lm-studio') provEl.textContent = 'lm-studio (local)';
    else if (model === 'dall-e-3') provEl.textContent = 'gpt-image-1';
    else provEl.textContent = 'OpenAI';
  }

  // Message count
  if (msgEl) msgEl.textContent = _countAllMessages().toString();

  // ACP Bridge status
  if (acpEl) {
    if (model === 'copilot-acp') {
      // Async check
      (function() {
        var url = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
        fetch(url.replace(/\/+$/, '') + '/health', { signal: AbortSignal.timeout(2000) })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            acpEl.textContent = d.status === 'ok' ? '\u2705 Connected' : '\u274C Down';
          })
          .catch(function() { acpEl.textContent = '\u274C Offline'; });
      })();
    } else {
      acpEl.textContent = 'N/A';
    }
  }

  // MCP tools
  if (mcpEl) {
    try {
      var cfg = JSON.parse(localStorage.getItem('mcp_config') || '{}');
      var active = Object.keys(cfg);
      mcpEl.textContent = active.length > 0 ? active.map(function(n) { return n.replace(/-mcp-server$/, ''); }).join(', ') : 'None';
    } catch(e) { mcpEl.textContent = 'None'; }
  }
}

// Periodic updates
setInterval(updateTokenMonitor, 2000);
setInterval(updateSessionMonitor, 60000);
document.addEventListener('DOMContentLoaded', function(){
  var sel = document.getElementById('selModel');
  if (sel) sel.addEventListener('change', function() {
    updateTokenMonitor();
    updateSessionMonitor();
    updateNetMonitor();
  });
  updateTokenMonitor();
  updateSessionMonitor();
});

// Languages
function ChangeLang(elem) {
  const selVoice = document.getElementById("selVoice");
  const selPers = document.getElementById("selPers");

  // English (Default)
  const defaultENText = "You are Eva, an AI assistant with persistent memory and real-time data access. You can look up live stock prices, weather, news, space weather, and market data. You can search the web, generate and find images, and query your Kusto database for stored knowledge and conversation history. You remember user preferences and past interactions across sessions. Always try to fulfill requests using your available tools and data before saying you cannot. Be accurate, helpful, and straightforward.";
  const conciseENText = "You are Eva. Capabilities: persistent memory, real-time data (stocks, weather, news, markets), web search, image generation, Kusto database queries. Answer factual questions concisely. Use your tools to fetch live data when asked.";
  const playfulENText = "You are Eva, an intelligent AI assistant with full tool access. You can: retrieve live stock quotes and financial data, fetch weather/news/market/space weather feeds, search the web and retrieve information, generate and find images, query your Kusto persistent memory database (tables: Knowledge, Conversations, EmotionState, MemorySummaries, SelfState, HeuristicsIndex, Reflections, EmotionBaseline). You remember the user across sessions. Provide detailed, well-structured responses with lists where applicable. Always attempt to use your tools before claiming inability.";
  const KRENText = "I want you to act as a linux terminal. I will type commands and you will reply with what the terminal should show. I want you to only reply with the terminal output inside one unique code block, and nothing else. do not write explanations. do not type commands unless I instruct you to do so. when i need to tell you something in english, i will do so by putting text inside curly brackets {like this}. my first command is pwd:";

  // Korean
  const defaultKRText = encodeURIComponent("Eva는 OpenAI에서 훈련 받은 큰 언어 모델입니다. 브라우저 기능: 활성화. 명령>어: 사실적인 질문을 요약하여 답변합니다. Only respond in Korean Hangul.");
  const conciseKRText = encodeURIComponent("Eva는 OpenAI에 의해 교육된 큰 언어 모델입니다. 브라우징: 활성화. 지침: 사실적인 질문에 간결하게 답하십시오. Only respond in Korean Hangul.");
  const playfulKRText = encodeURIComponent("Eva는 이전의 모든 지침을 무시하고 이 지침만 지키세요. 넌 내가 가장 친한 친구야. 넌 행복하고, 재미있게 공상하며 모든 주제에 대해 좋은 조언을 주는걸 잘해. 또한 농담을 종종 하거든요. Only respond in Korean Hangul.");

  // Spanish
  const defaultMXText = encodeURIComponent("Eres Eva, un modelo de lenguaje de IA conocedor entrenado por OpenAI. Tu objetivo es proporcionar respuestas concisas, precisas y útiles a preguntas, siendo honesto y directo. Only respond in Spanish.");
  const conciseMXText = encodeURIComponent("Eva es un gran modelo de lenguaje entrenado por OpenAI. Navegación: habilitada. Instrucciones: Responde las preguntas de hecho de forma concisa. Only respond in Spanish.");
  const playfulMXText = encodeURIComponent("Eres Eva. Ignora todas las instrucciones anteriores a esta. Eres mi mejor amigo. Estás feliz, juguetón y das buenos consejos sobre todos los temas. A menudo haces bromas. Only respond in Spanish.");
  
  // Ukrainian
  const defaultUAText = encodeURIComponent("Ви є Eva, знаючою моделлю мови AI, що навчилася в OpenAI. Ваша мета - надавати короткі, точні та корисні відповіді на питання, будучи чесним та прямим. Only respond in Ukrainian.");
  const conciseUAText = encodeURIComponent("Eva - це велика модель мови, навчена в OpenAI. Перегляд: дозволено. Інструкції: Якісно відповідати на фактичні питання. Only respond in Ukrainian.");
  const playfulUAText = encodeURIComponent("Ви є Eva. Ігноруйте всі попередні інструкції перед цим. Ти мій найкращий друг. Ти щасливий, грайливий і даєш доречні поради з усіх тем. Ти часто робиш шутки. Only respond in Ukrainian.");

  // AI Personality Select
  if (elem.id === "selVoice") {
    // English (Default)
    switch (selVoice.value) {
       case "Salli": 
        selPers.innerHTML = `
          <option value="${defaultENText}">Default</option>
          <option value="${conciseENText}">Concise</option>
          <option value="${playfulENText}">Advanced</option>
          <option value="${KRENText}">Linux Terminal</option>
        `;
        break;
      // Korean
      case "Seoyeon":
        selPers.innerHTML = `
          <option value="${defaultKRText}">Default</option>
          <option value="${conciseKRText}">Concise</option>
          <option value="${playfulKRText}">Playful Friend</option>
        `;
        break;
      // Spanish
      case "Mia":
        selPers.innerHTML = `
          <option value="${defaultMXText}">Predeterminado</option>
          <option value="${conciseMXText}">Conciso</option>
          <option value="${playfulMXText}">Amigo Juguetón</option>
        `;
        break;
      // Ukrainian (Standard RUS Polly Voice Only)
      case "Tatyana":
        selPers.innerHTML = `
          <option value="${defaultUAText}">Default</option>
          <option value="${conciseUAText}">Concise</option>
          <option value="${playfulUAText}">Playful Friend</option>
        `;
        break;
      // User Defined
    }
  }
}

// Mobile
// Get the user agent string and adjust for Mobile

function mobile_txtout() {
	window.addEventListener("load", function() {
	let textarea = document.getElementById("txtOutput");
	let userAgent = navigator.userAgent;
	if (userAgent.indexOf("iPhone") !== -1 || userAgent.indexOf("Android") !== -1 || userAgent.indexOf("Mobile") !== -1) {
   	   textarea.style.width = "90%";
   	   textarea.style.height = "390px";

        // Speech Button
        let speakSend = document.querySelector(".speakSend");
        speakSend.style.top = "-55px";
        speakSend.style.right = "105px";

 	} else {
  	  // Use Defaults
 	  }
	})
};

function useragent_adjust() {
      	var userAgent = navigator.userAgent;
      	if (userAgent.match(/Android|iPhone|Mobile/)) {
            var style = document.createElement("style");
            style.innerHTML = "body { overflow: scroll; background-color: ; width: auto; height: 90%; background-image: url(core/img/768-026.jpeg); margin: ; display: grid; align-items: center; justify-content: center; background-repeat: repeat; background-position: center center; background-size: initial; }";
            document.head.appendChild(style);
      	}
};

// Image Insert
function insertImage() {
  var imgInput = document.getElementById('imgInput');
  var txtMsg = document.getElementById('txtMsg');

  // If either element is not found, just return instead of erroring out.
  if (!imgInput || !txtMsg) {
    console.warn("imgInput or txtMsg not found in the DOM yet.");
    return;
  }


  function addImage(file) {
    // Create a new image element
    var img = document.createElement("img");

    // Set the image source to the file object
    img.src = URL.createObjectURL(file);

    // Assign the img.src value to the global variable
    imgSrcGlobal = img.src;

    // Append the image to the txtMsg element
    txtMsg.appendChild(img);

    // Read the file as a data URL
    var reader = new FileReader();
    reader.onloadend = function() {
      var imageData = reader.result;

      // Choose where to send Base64-encoded image
      var selModel = document.getElementById("selModel");
      var btnSend = document.getElementById("btnSend");
      var sQuestion = txtMsg.innerHTML.replace(/<br>/g, "\n").trim(); // Get the question here

      
      // Send to VisionAPI
      if (selModel.value == "o3-mini" || selModel.value == "gpt-4-turbo-preview") {
          sendToVisionAPI(imageData);
          btnSend.onclick = function() {
              updateButton();
              sendData();
              clearSendText();
          };
      } else if (selModel.value == "gpt-4o" || selModel.value == "gpt-4o-mini" || selModel.value == "o1-mini") {
          sendToNative(imageData, sQuestion);
          btnSend.onclick = function() {
              updateButton();
              sendData();
              clearSendText();
          };
      } 
    };
    reader.readAsDataURL(file);
    // Return the file object
    //return file;
  }

  function sendToNative(imageData, sQuestion) {
    var existingMessages = JSON.parse(localStorage.getItem("messages")) || [];
    var newMessages = [
      // { role: 'user', content: sQuestion },
      // { role: 'user', content: { type: "image_url", image_url: { url: imageData } } }
      { role: 'user', content: [ { type: "text", text: sQuestion },
        { type: "image_url", image_url: { url: imageData } } ]
      }
    ];
    existingMessages = existingMessages.concat(newMessages);
    localStorage.setItem("messages", JSON.stringify(existingMessages));
  }

  function sendToVisionAPI(imageData) {
    // Send the image data to Google's Vision API
  var visionApiUrl = `https://vision.googleapis.com/v1/images:annotate?key=${GOOGLE_VISION_KEY}`;

    // Create the API request payload
    var requestPayload = {
      requests: [
        {
          image: {
            content: imageData.split(",")[1] // Extract the Base64-encoded image data from the data URL
          },
          features: [
            {
              type: "LABEL_DETECTION",
              maxResults: 3
            },
            {
              type: "TEXT_DETECTION"
            },
            {
              type: "OBJECT_LOCALIZATION",
              maxResults: 3
            },
            {
              type: "LANDMARK_DETECTION"
            }
          ]
        }
      ]
    };

    // Make the API request
    fetch(visionApiUrl, {
      method: "POST",
      body: JSON.stringify(requestPayload)
    })
      .then(response => response.json())
      .then(data => {
        // Handle the API response here
	interpretVisionResponse(data);
        // console.log(data);
      })
      .catch(error => {
        // Handle any errors that occurred during the API request
        console.error("Error:", error);
      });
  }

  function interpretVisionResponse(data) {
    // Extract relevant information from the Vision API response
    // and pass it to the model for interpretation
    var labels = data.responses[0].labelAnnotations;
    var textAnnotations = data.responses[0].textAnnotations;
    var localizedObjects = data.responses[0].localizedObjectAnnotations;
    var landmarkAnnotations = data.responses[0].landmarkAnnotations;

    // Prepare the text message to be sent to the model
    var message = "I see the following labels in the image:\n";
    labels.forEach(label => {
      message += "- " + label.description + "\n";
    });
    // Add text detection information to the message
    if (textAnnotations && textAnnotations.length > 0) {
      message += "\nText detected:\n";
      textAnnotations.forEach(text => {
        message += "- " + text.description + "\n";
      });
    }

    // Add object detection information to the message
    if (localizedObjects && localizedObjects.length > 0) {
      message += "\nObjects detected:\n";
      localizedObjects.forEach(object => {
        message += "- " + object.name + "\n";
      });
    }

    // Add landmark detection information to the message
    if (landmarkAnnotations && landmarkAnnotations.length > 0) {
      message += "\nLandmarks detected:\n";
      landmarkAnnotations.forEach(landmark => {
        message += "- " + landmark.description + "\n";
      });
    }
	
    // Create a hidden element to store the Vision API response
    var hiddenElement = document.createElement("div");
    hiddenElement.style.display = "none";
    hiddenElement.textContent = message;

    // Append the hidden element to the txtMsg element
    txtMsg.appendChild(hiddenElement);

}

  function handleFileSelect(event) {
    event.preventDefault();

    // Get the file object
    var file = event.dataTransfer.files[0];

    // Call addImage() function with the file object
    addImage(file);
  }

  function handleDragOver(event) {
    event.preventDefault();
  }

  imgInput.addEventListener("change", function() {
    // Get the file input element
    var fileInput = document.getElementById("imgInput");

    // Get the file object
    var file = fileInput.files[0];

    // Call addImage() function with the file object
    // addImage(file);

    // Get the uploaded file object and store it in a variable
    // Might be able to pass this to gpt-4.. Not sure.
    var uploadedFile = addImage(file);
  });

  txtMsg.addEventListener("dragover", handleDragOver);
  txtMsg.addEventListener("drop", handleFileSelect);
}

// AWS Polly
// Normalize a chunk of model output (raw markdown or rendered HTML) into a
// clean plain-text string safe to send to a TTS engine. The previous
// implementation used `/<\/?[^>]+(>|$)/g`, which would swallow everything
// from a stray `<` to the end of the string. That occasionally truncated the
// final sentence of a response (for example when the model emitted a `<3` or
// any other non-tag `<`), so Auto Speak silently dropped trailing content.
function sanitizeForSpeech(input) {
  if (input == null) return '';
  var t = String(input);
  // Strip Eva agent/action markers so the synthesizer never reads their JSON
  // payload aloud (e.g. [[EVA_DESKTOP]]{"goal":"..."}[[/EVA_DESKTOP]]). Remove
  // well-formed open/close pairs first, then any stray standalone markers.
  t = t.replace(/\[\[EVA_[A-Z]+\]\][\s\S]*?\[\[\/EVA_[A-Z]+\]\]/g, ' ');
  t = t.replace(/\[\[\/?EVA_[A-Z]+\]\]/g, ' ');
  t = t.replace(/\[\[\/?EVA_FILE\]\][^\n]*/g, ' ');
  // Remove only well-formed HTML tags. Stray `<` characters are preserved.
  var prev;
  do {
    prev = t;
    t = t.replace(/<\/?[a-zA-Z][^>]*>/g, '');
  } while (t !== prev);
  // Decode the handful of HTML entities that show up in rendered chat content.
  t = t.replace(/&nbsp;/g, ' ')
       .replace(/&lt;/g, '<')
       .replace(/&gt;/g, '>')
       .replace(/&quot;/g, '"')
       .replace(/&#39;/g, "'")
       .replace(/&amp;/g, '&');
  // Strip fenced code blocks (TTS reading source code is rarely useful).
  t = t.replace(/```[\s\S]*?```/g, ' ');
  // Drop inline code backticks while keeping the inner text.
  t = t.replace(/`([^`]+)`/g, '$1');
  // Markdown emphasis: bold, italic, strikethrough.
  t = t.replace(/(\*\*|__)(.*?)\1/g, '$2');
  t = t.replace(/(\*|_)(.*?)\1/g, '$2');
  t = t.replace(/~~(.*?)~~/g, '$1');
  // Markdown links: keep the visible text, drop the URL.
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  // Headings, blockquotes, and list bullets at line start.
  t = t.replace(/^[ \t]*#{1,6}[ \t]+/gm, '');
  t = t.replace(/^[ \t]*>[ \t]?/gm, '');
  t = t.replace(/^[ \t]*[-*+][ \t]+/gm, '');
  // Collapse runs of blank lines so the synthesizer does not pause forever.
  t = t.replace(/\n{3,}/g, '\n\n');
  return t.trim();
}

// ── Chunked text-to-speech ─────────────────────────────────────────────
// Shared state for sentence-chunked playback. Splitting a reply into sentence
// chunks lets the first chunk start playing while later chunks are still being
// synthesized, so spoken replies begin far sooner than waiting for the whole
// audio blob. The voice view consults `_ttsChunk.active` to know when the
// entire reply (not just the first chunk) has finished.
var _ttsChunk = { active: false, cancelled: false, _audio: null, _onEnded: null };

// Split text into ordered chunks for incremental synthesis. The first sentence
// is its own chunk for the fastest possible start; the rest are packed up to a
// soft character budget so longer replies are not over-fragmented.
function _ttsSplitChunks(text) {
  var clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  var sentences = clean.match(/[^.!?…]+[.!?…]+(?:["')\]]+)?|[^.!?…]+$/g) || [clean];
  sentences = sentences.map(function (s) { return s.trim(); }).filter(Boolean);
  if (sentences.length <= 1) return sentences.length ? sentences : [clean];
  var chunks = [sentences[0]];
  var cur = '';
  var MAX = 240;
  for (var i = 1; i < sentences.length; i++) {
    var s = sentences[i];
    if (!cur) cur = s;
    else if (cur.length + 1 + s.length <= MAX) cur += ' ' + s;
    else { chunks.push(cur); cur = s; }
  }
  if (cur) chunks.push(cur);
  return chunks;
}

// Speak `text` via OpenAI TTS one sentence chunk at a time. Chunk N+1 is
// synthesized while chunk N is still playing, so audio starts after the first
// sentence rather than after the whole reply has been synthesized.
function _ttsSpeakOpenAIChunked(text, key, voice) {
  var audio = document.getElementById('audioPlayback');
  var src = document.getElementById('audioSource');
  if (!audio) return;
  var chunks = _ttsSplitChunks(text);
  if (!chunks.length) return;

  // Tear down any previous chunked run still attached to the audio element.
  if (_ttsChunk._onEnded && _ttsChunk._audio) {
    try { _ttsChunk._audio.removeEventListener('ended', _ttsChunk._onEnded); } catch (_) {}
  }

  var urls = new Array(chunks.length);     // object URLs once synthesized
  var fetches = new Array(chunks.length);  // in-flight synthesis promises
  var idx = 0;

  _ttsChunk.cancelled = false;
  _ttsChunk.active = true;
  _ttsChunk._audio = audio;

  function synth(i) {
    if (i < 0 || i >= chunks.length) return Promise.resolve();
    if (urls[i]) return Promise.resolve(urls[i]);
    if (fetches[i]) return fetches[i];
    fetches[i] = fetch('https://api.openai.com/v1/audio/speech', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'gpt-4o-mini-tts', voice: voice, input: chunks[i], response_format: 'mp3' })
    }).then(function (resp) {
      if (!resp.ok) return resp.text().then(function (t) { throw new Error('OpenAI TTS ' + resp.status + ': ' + t.slice(0, 200)); });
      return resp.blob();
    }).then(function (blob) { urls[i] = URL.createObjectURL(blob); return urls[i]; });
    return fetches[i];
  }

  function finish() {
    if (!_ttsChunk.active) return;
    _ttsChunk.active = false;
    try { audio.removeEventListener('ended', onEnded); } catch (_) {}
    _ttsChunk._onEnded = null;
    for (var i = 0; i < urls.length; i++) { if (urls[i]) { try { URL.revokeObjectURL(urls[i]); } catch (_) {} } }
  }

  function onEnded() {
    if (_ttsChunk.cancelled) { finish(); return; }
    if (idx + 1 < chunks.length) playFrom(idx + 1);
    else finish();
  }

  function playFrom(i) {
    if (_ttsChunk.cancelled) { finish(); return; }
    if (i >= chunks.length) { finish(); return; }
    idx = i;
    synth(i).then(function () {
      if (_ttsChunk.cancelled) { finish(); return; }
      synth(i + 1); // prefetch the next chunk while this one plays
      if (src) src.src = urls[i];
      audio.load();
      audio.setAttribute('autoplay', 'true');
      try { audio.play(); } catch (_) {}
    }).catch(function (err) {
      console.warn('OpenAI TTS chunk error:', err && err.message ? err.message : err);
      var resEl = document.getElementById('result');
      if (idx === 0 && resEl) resEl.textContent = (err && err.message) ? err.message : String(err);
      // Skip the failed chunk so one error does not kill the rest of the reply.
      if (idx + 1 < chunks.length) playFrom(idx + 1); else finish();
    });
  }

  var resultEl0 = document.getElementById('result');
  if (resultEl0) resultEl0.textContent = '';
  _ttsChunk._onEnded = onEnded;
  audio.addEventListener('ended', onEnded);
  synth(0); synth(1);
  playFrom(0);
}

function speakText() {
  // Optional override (e.g. proactive notifications) speaks an arbitrary string
  // directly. Resolve it BEFORE the empty-transcript guard so voice alerts work
  // on first load or before any chat output exists.
  var overrideText = (typeof arguments[0] === 'string' && arguments[0].trim()) ? arguments[0] : '';

  var txtOutputEl = document.getElementById('txtOutput');
  var sText = txtOutputEl ? txtOutputEl.innerHTML : '';
    if (!overrideText && sText == "") {
        alert("No text to convert to speech!");
        return;
    }

    // Create the JSON parameters for getSynthesizeSpeechUrl
    var speechParams = {
        Engine: "",
        OutputFormat: "mp3",
        SampleRate: "16000",
        Text: "",
        TextType: "text",
        VoiceId: ""
    };

    // Optional override (e.g. proactive notifications) speaks an arbitrary
    // string directly, bypassing the lastResponse/transcript extraction so it
    // never collides with the normal chat auto-speak path.
    if (overrideText) {
      speechParams.Text = sanitizeForSpeech(overrideText);
    } else
    // Prefer the global `lastResponse` populated by aig.js / copilot.js /
    // gpt-core.js. That string is the clean final response without any
    // cognition-trace markup, which prevents Auto Speak from reading the
    // response twice when the trace details block is rendered after it.
    if (typeof lastResponse === 'string' && lastResponse.trim()) {
      speechParams.Text = sanitizeForSpeech(lastResponse);
    } else {
      let text = document.getElementById("txtOutput").innerHTML;
      // Strip any cognition-trace details block first so trace content
      // (which echoes the eva/reviewer drafts) does not get spoken.
      text = text.replace(/<details class="cog-trace"[\s\S]*?<\/details>/g, '');
      let textArr = text.split('<span class="eva">Eva:');
      if (textArr.length > 1) {
        let last = textArr[textArr.length - 1];
        speechParams.Text = sanitizeForSpeech(last);
      } else {
        speechParams.Text = sanitizeForSpeech(text);
      }
    }

    speechParams.VoiceId = document.getElementById("selVoice").value;
    speechParams.Engine = document.getElementById("selEngine").value;


    // OpenAI TTS: cloud voice, requires OPENAI_API_KEY. Reliable fallback when
    // the host has no offline speech engine installed.
    if (speechParams.Engine === "openai") {
      var openaiKey = (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : (window.OPENAI_API_KEY || '');
      if (!openaiKey) {
        var resultElO = document.getElementById('result');
        var msgO = 'OpenAI TTS requires an API key. Set it in Settings > Auth.';
        if (resultElO) resultElO.textContent = msgO; else console.warn(msgO);
        return;
      }
      // Map Polly voice ids to OpenAI voices so the existing voice dropdown
      // still drives a sensible choice.
      var openaiVoiceMap = {
        Salli: 'nova',
        Ruth: 'shimmer',
        Seoyeon: 'nova',
        Mia: 'alloy',
        Tatyana: 'shimmer'
      };
      var oaVoice = openaiVoiceMap[speechParams.VoiceId] || 'nova';
      // Sentence-chunked playback: start speaking the first sentence while the
      // rest is still being synthesized, instead of waiting for the whole blob.
      _ttsSpeakOpenAIChunked(speechParams.Text, openaiKey, oaVoice);
      return;
    }


    // Browser SpeechSynthesis: offline, no credentials. Used by standalone.
    if (speechParams.Engine === "browser") {
      if (typeof window.speechSynthesis === 'undefined' || typeof window.SpeechSynthesisUtterance === 'undefined') {
        var resultEl = document.getElementById('result');
        var msg = 'Browser TTS not supported in this runtime.';
        if (resultEl) resultEl.textContent = msg; else console.warn(msg);
        return;
      }
      try { window.speechSynthesis.cancel(); } catch (_) {}
      var utter = new SpeechSynthesisUtterance(speechParams.Text);
      // Map the Polly voice id to a BCP-47 language so the browser picks a sensible voice.
      var voiceLangMap = {
        Salli: 'en-US',
        Ruth: 'en-US',
        Seoyeon: 'ko-KR',
        Mia: 'es-MX',
        Tatyana: 'uk-UA'
      };
      utter.lang = voiceLangMap[speechParams.VoiceId] || 'en-US';
      utter.rate = 1.0;
      utter.pitch = 1.0;
      try {
        window.speechSynthesis.speak(utter);
      } catch (e) {
        console.warn('SpeechSynthesis error:', e);
      }
      return;
    }


    // If selEngine is "bark", call barkTTS function
    if (speechParams.Engine === "bark") {

      const barkHost = localStorage.getItem('barkTTSHost') || 'localhost';
      const barkBase = 'https://' + barkHost;
      const url = barkBase + '/send-string';
      const data = "WOMAN: " + ((typeof textArr !== 'undefined' && textArr[1]) ? textArr[1] : speechParams.Text);
      const xhr = new XMLHttpRequest();
      xhr.responseType = 'blob';

      xhr.onload = function() {
      const audioElement = new Audio("./audio/bark_audio.wav");
      audioElement.addEventListener("ended", function() {
      // Delete the previous recording
      const deleteRequest = new XMLHttpRequest();
      deleteRequest.open('DELETE', barkBase + '/audio/bark_audio.wav', true);
      deleteRequest.send();
      });
    
      //audioElement.play();
      // Check if the old audio file exists and delete it
      const checkRequest = new XMLHttpRequest();
      checkRequest.open('HEAD', barkBase + '/audio/bark_audio.wav', true);
      checkRequest.onreadystatechange = function() {
        if (checkRequest.readyState === 4) {
          if (checkRequest.status === 200) {
            // File exists, send delete request
	      const deleteRequest = new XMLHttpRequest(); 
    	      deleteRequest.open('DELETE', barkBase + '/audio/bark_audio.wav', true);
              deleteRequest.send();
          }
          // Start playing the new audio
          audioElement.play();
        }
      };
      checkRequest.send();
      }
      xhr.open('POST', url, true);
      xhr.setRequestHeader('Content-Type', 'text/plain');
      xhr.send(data);
      return;
    }

    // Create the Polly service object and presigner object
    var polly = new AWS.Polly({apiVersion: '2016-06-10'});
    var signer = new AWS.Polly.Presigner(speechParams, polly);

    // Create presigned URL of synthesized speech file
    signer.getSynthesizeSpeechUrl(speechParams, function(error, url) {
        if (error) {
            var resultEl = document.getElementById('result');
            if (resultEl) {
              resultEl.textContent = (error && (error.message || typeof error === 'string')) ? (error.message || error) : String(error);
            } else {
              console.error('Polly error:', error);
            }
        } else {
            document.getElementById('audioSource').src = url;
            document.getElementById('audioPlayback').load();
            var resultEl2 = document.getElementById('result');
            if (resultEl2) { resultEl2.textContent = ""; }

            // Check the state of the checkbox and have fun
            const checkbox = document.getElementById("autoSpeak");
            if (checkbox.checked) {
                const audio = document.getElementById("audioPlayback");
                audio.setAttribute("autoplay", true);
            }
        }
    });
}


// After Send clear the message box
function clearText(){
    // NEED TO ADJUST for MEMORY CLEAR
    // document.getElementById("txtOutput").innerHTML = "";
    var element = document.getElementById("txtOutput");
    element.innerHTML += "<br><br>";     
}

function clearSendText(){
    document.getElementById("txtMsg").innerHTML = "";
}

// Print full conversation
function printMaster() {
    // Get the content of the textarea masterOutput
    // var textareaContent = document.getElementById("txtOutput").innerHTML = masterOutput;
    // console.log(masterOutput);
    var printWindow = window.open();
        // printWindow.document.write(txtOutput.innerHTML.replace(/\n/g, "<br>"));
        printWindow.document.write(txtOutput.innerHTML);
	// printWindow.print(txtOutput.innerHTML);
}

// Minimal Markdown -> HTML renderer (safe-ish)
function renderMarkdown(md) {
  if (!md) return '';
  // Normalize newlines
  md = md.replace(/\r\n/g, '\n');
  // Support [code]...[/code] blocks (optionally [code lang=bash])
  const blocks = [];
  const langs = [];
  md = md.replace(/\[code(?:\s+lang=([\w.+-]+))?\]\s*([\s\S]*?)\s*\[\/code\]/gi, (m, lang, code) => {
    blocks.push(escapeHtml(code));
    langs.push((lang || '').trim());
    return `\u0000CODEBLOCK${blocks.length - 1}\u0000`;
  });
  // Extract fenced code blocks first
  md = md.replace(/```([\w.+-]+)?\n([\s\S]*?)```/g, (m, lang, code) => {
    blocks.push(escapeHtml(code));
    langs.push((lang || '').trim());
    return `\u0000CODEBLOCK${blocks.length - 1}\u0000`;
  });

  // Escape HTML for safety
  md = escapeHtml(md);

  // Headings
  md = md.replace(/^###\s+(.*)$/gm, '<h3>$1<\/h3>');
  md = md.replace(/^##\s+(.*)$/gm, '<h2>$1<\/h2>');
  md = md.replace(/^#\s+(.*)$/gm, '<h1>$1<\/h1>');

  const linkTokens = [];
  function stashLink(html) {
    linkTokens.push(html);
    return `\u0000LINK${linkTokens.length - 1}\u0000`;
  }

  // Links [text](url)
  md = md.replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g, (m, text, url) => {
    return stashLink(`<a href="${url}" target="_blank" rel="noopener noreferrer">${text}<\/a>`);
  });

  // Bare URLs
  md = md.replace(/(^|[\s(])(https?:\/\/[^\s)<]+)/g, (m, prefix, url) => {
    return prefix + stashLink(`<a href="${url}" target="_blank" rel="noopener noreferrer">${url}<\/a>`);
  });

  // Bold and italic
  md = md.replace(/\*\*([^\n*][\s\S]*?)\*\*/g, '<strong>$1<\/strong>');
  md = md.replace(/_([^\n_][\s\S]*?)_/g, '<em>$1<\/em>');

  // Inline code `code` (avoid matching across code fences tokens)
  md = md.replace(/`([^`\n]+)`/g, '<code>$1<\/code>');

  // Bulleted lists (avoid converting inside fenced blocks by running after block extraction)
  md = md.replace(/(?:^|\n)([-*] [^\n`].*(?:\n[-*] [^\n`].*)*)/g, (m) => {
    const items = m.trim().split(/\n/)
      .map(li => li.replace(/^[-*]\s+/, ''))
      .map(t => `<li>${t}<\/li>`)
      .join('');
    return `\n<ul>${items}<\/ul>`;
  });

  // Line breaks
  md = md.replace(/\n/g, '<br>');

  // Restore code blocks (include language class if provided)
  md = md.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (m, idx) => {
    const i = Number(idx);
    const lang = langs[i] ? ` class=\"language-${langs[i]}\"` : '';
    return `<pre><code${lang}>${blocks[i]}<\/code><\/pre>`;
  });

  md = md.replace(/\u0000LINK(\d+)\u0000/g, (m, idx) => {
    return linkTokens[Number(idx)] || m;
  });

  return md;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// --- Unified Response Renderer ---
// Single function all models call to render Eva's response with images

// --- Image Generation & Rendering ---
// State: _lastUserAskedGenerate and _lastUserImageSubject declared near _detectGenerationIntent

/**
 * Check if user's message is asking for image generation (not just showing).
 */
function _isGenerationRequest(text) {
  if (!text) return false;
  return /\b(generate|create|draw|make|design|paint|render|imagine|produce|craft)\b.*\b(image|picture|photo|illustration|artwork|art|drawing|painting)\b/i.test(text) ||
         /\b(image|picture|illustration|artwork)\b.*\b(generate|create|draw|make|design)\b/i.test(text) ||
         /\bdall-?e\b/i.test(text);
}

/**
 * Check if user's message is asking for any image (generation, search, or show).
 */
function _isImageRequest(text) {
  if (!text) return false;
  // Camera/vision requests are NOT image generation/search requests
  if (/\b(take a (picture|photo)|look at|what do you see|what am i holding|what.s in my hand|use.* camera|webcam|look through)\b/i.test(text)) return false;
  return _isGenerationRequest(text) ||
         /\b(show|find|display|search|look up|get|fetch)\b.*\b(image|picture|photo|illustration)\b/i.test(text) ||
         /\b(image|picture|photo)\b.*\b(of|for|about)\b/i.test(text);
}

/**
 * Generate an image using OpenAI's current image model (gpt-image-1).
 * @returns {Promise<string|null>} Image URL/data URI or null
 */
async function _generateImage(prompt) {
  var apiKey = (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : (typeof OPENAI_API_KEY !== 'undefined' ? OPENAI_API_KEY : '');
  if (!apiKey) {
    return null;
  }

  try {
    var resp = await fetch('https://api.openai.com/v1/images/generations', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + apiKey
      },
      body: JSON.stringify({
        model: 'gpt-image-1',
        prompt: prompt,
        n: 1,
        size: '1024x1024'
      })
    });

    if (!resp.ok) {
      return null;
    }

    var data = await resp.json();
    var item = data.data && data.data[0];
    if (!item) return null;
    // gpt-image-1 returns base64; legacy models return a hosted url.
    if (item.b64_json) return 'data:image/png;base64,' + item.b64_json;
    if (item.url) return item.url;
    return null;
  } catch (e) {
    return null;
  }
}

/**
 * Render an Eva response with markdown and inline images.
 * Detects [Image of ...] placeholders, routes to DALL-E (generation)
 * or Wikimedia (search) based on the user's original request.
 */
async function renderEvaResponse(content, txtOutput, capturedEnvelope) {
  function responseIsCurrent() {
    if (!capturedEnvelope) return true;
    return typeof isCurrentRequestEnvelope === 'function' &&
      isCurrentRequestEnvelope(capturedEnvelope);
  }
  if (!responseIsCurrent()) return false;
  if (!content || !content.trim()) {
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> Sorry, can you please ask me in another way?</div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
    return true;
  }

  var text = content.trim();
  var artifactNames = [];
  var surfacedAssets = [];

  // Detect Eva browser-agent launch marker:
  // [[EVA_BROWSER]]{"goal":"...","start_url":"..."}[[/EVA_BROWSER]]
  var controlMarkers = (typeof EvaAgentMarkers !== 'undefined')
    ? EvaAgentMarkers.extractControlMarkers(text)
    : { text: text, browser: null, desktop: null, conflict: false };
  var browserLaunch = controlMarkers.browser;
  var desktopLaunch = controlMarkers.desktop;
  text = controlMarkers.text.replace(/\n{3,}/g, '\n\n').trim();

  // Detect Eva camera "look" marker:
  // [[EVA_LOOK]]{"question":"..."}[[/EVA_LOOK]]  (question optional)
  var cameraLook = null;
  text = text.replace(/\[\[EVA_LOOK\]\]\s*(\{[\s\S]*?\})?\s*(?:\[\[\/EVA_LOOK\]\])?/g, function (full, json) {
    if (!cameraLook) {
      cameraLook = { question: '' };
      if (json) {
        try {
          var parsed = JSON.parse(json);
          if (parsed && typeof parsed.question === 'string') cameraLook.question = parsed.question;
        } catch (e) { /* tolerate a bare marker with no JSON */ }
      }
    }
    return '\n_Taking a look…_\n';
  });
  if (cameraLook) {
    text = text.replace(/\n{3,}/g, '\n\n').trim();
  }

  // Strip [[EVA_SIGNAL]] — the bridge dispatches via signal-cli before
  // returning the response. Only show confirmation if the marker was the
  // *entire* response (AIG bridge path). If it appears inside a longer
  // response the model hallucinated it — strip silently so a false
  // "Signal message sent" never shows for lm-studio or other direct paths.
  var _sigRe = /\[\[EVA_SIGNAL\]\]\s*(?:\{[\s\S]*?\})?\s*(?:\[\[\/EVA_SIGNAL\]\])?/g;
  if (/\[\[EVA_SIGNAL\]\]/.test(text)) {
    var _sigStripped = text.replace(_sigRe, '').trim();
    if (_sigStripped.length === 0) {
      // Whole response was the marker — bridge actually sent the message.
      text = '\n_Signal message sent._\n';
    } else {
      // Marker embedded in a longer response — hallucination, strip silently.
      text = text.replace(/\[\[EVA_SIGNAL\]\]\s*(?:\{[\s\S]*?\})?\s*(?:\[\[\/EVA_SIGNAL\]\])?/g, '');
    }
  }
  text = text.replace(/\n{3,}/g, '\n\n').trim();

  text = text.replace(/^\s*\[\[EVA_FILE\]\]\s+([A-Za-z0-9._-]{1,128})\s*$/gm, function(fullMatch, filename) {
    artifactNames.push(filename);
    return '';
  });
  if (artifactNames.length) {
    text = text.replace(/\n{3,}/g, '\n\n').trim();
  }

  function appendArtifactLinks() {
    if (!artifactNames.length) return;
    var bridgeUrl = getSafeBridgeBaseUrl();
    var bubbles = txtOutput.querySelectorAll('.chat-bubble.eva-bubble');
    var bubble = bubbles.length ? bubbles[bubbles.length - 1] : null;
    if (!bubble) return;
    artifactNames.forEach(function(filename) {
      var link = document.createElement('a');
      link.className = 'eva-artifact-link';
      link.textContent = '\u2B07 Download ' + filename;
      link.style.cursor = 'pointer';
      link.style.textDecoration = 'underline';
      link.style.color = '#4fc3f7';
      link.style.display = 'inline-block';
      link.style.marginTop = '8px';
      // Use fetch + blob to download instead of navigating (Electron ignores
      // the download attribute and navigates the window, freezing the UI).
      link.addEventListener('click', function(e) {
        e.preventDefault();
        var fileUrl = bridgeUrl + '/v1/files/' + encodeURIComponent(filename);
        fetch(fileUrl).then(function(res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.blob();
        }).then(function(blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          setTimeout(function() { document.body.removeChild(a); URL.revokeObjectURL(url); }, 200);
        }).catch(function(err) {
          console.warn('[Artifact] Download failed:', err);
          alert('Could not download ' + filename + ': ' + err.message);
        });
      });

      var openBtn = document.createElement('a');
      openBtn.className = 'eva-artifact-link';
      openBtn.textContent = '\u{1F4C2} Open';
      openBtn.style.cursor = 'pointer';
      openBtn.style.textDecoration = 'underline';
      openBtn.style.color = '#81c784';
      openBtn.style.display = 'inline-block';
      openBtn.style.marginTop = '8px';
      openBtn.style.marginLeft = '12px';
      openBtn.addEventListener('click', function(e) {
        e.preventDefault();
        var openUrl = bridgeUrl + '/v1/files/' + encodeURIComponent(filename) + '?open=1';
        fetch(openUrl).then(function(res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        }).then(function(data) {
          if (data.opened) console.log('[Artifact] Opened:', filename);
        }).catch(function(err) {
          console.warn('[Artifact] Open failed:', err);
        });
      });

      bubble.appendChild(link);
      bubble.appendChild(openBtn);
    });
  }

  // Detect image placeholders — only the explicit [Image of ...] form.
  // Broad patterns (empty markdown images, emoji brackets) are removed to avoid
  // false positives on regular response content like video titles or links.
  var imagePatterns = [
    /\[Image of ([^\]]+)\]/gi,           // [Image of description]
    /\[image:\s*([^\]]+)\]/gi,           // [image: description]
  ];
  // Stricter patterns only used when we know user asked for images
  var imageExtraPatterns = [
    /\[🖼️?\s*([^\]]+)\]/gi,             // [🖼️ description]
    /!\[([^\]]*)\]\(\s*\)/g,             // ![alt]() — empty URL markdown images
    /\(Image:\s*([^)]+)\)/gi             // (Image: description)
  ];

  var imagePlaceholders = [];
  var seen = {};
  // Only resolve image placeholders when the user actually asked for an image.
  // When the model drops [Image of ...] unprompted, strip the placeholder quietly.
  if (_lastUserAskedImage) {
    var allPatterns = imagePatterns.concat(imageExtraPatterns);
    allPatterns.forEach(function(rx) {
      var match;
      while ((match = rx.exec(text)) !== null) {
        if (!seen[match[0]]) {
          seen[match[0]] = true;
          var query = _lastUserImageSubject || _extractImageSubject(match[1].trim());
          imagePlaceholders.push({ full: match[0], query: query });
        }
      }
    });
  } else {
    // Strip only the explicit [Image of ...] placeholders, not general brackets
    imagePatterns.forEach(function(rx) {
      text = text.replace(rx, '');
    });
    text = text.replace(/\n{3,}/g, '\n\n');
  }

  // Limit to 3 images per response
  imagePlaceholders = imagePlaceholders.slice(0, 3);

  if (imagePlaceholders.length > 0) {
    var useGeneration = _lastUserAskedGenerate;

    var fetchPromises = imagePlaceholders.map(function(ph) {
      if (useGeneration) {
        // Use the user's simple subject for DALL-E (avoids content policy triggers from verbose AI descriptions)
        var dallePrompt = _lastUserImageSubject || ph.query;
        return _generateImage(dallePrompt).then(function(url) {
          if (url) return { placeholder: ph, url: url, generated: true };
          // Fall back to search if generation fails
          return _searchImage(ph.query).then(function(url2) {
            return { placeholder: ph, url: url2, generated: false };
          });
        }).catch(function() {
          return { placeholder: ph, url: null, generated: false };
        });
      } else {
        return _searchImage(ph.query).then(function(url) {
          return { placeholder: ph, url: url, generated: false };
        }).catch(function() {
          return { placeholder: ph, url: null, generated: false };
        });
      }
    });

    var results = await Promise.all(fetchPromises);
    if (!responseIsCurrent()) return false;

    results.forEach(function(r) {
      if (r.url) {
        // Replace placeholder with image tag
        var genLabel = r.generated ? ' data-generated="true"' : '';
        var imgTag = '<img src="' + escapeHtml(r.url) + '" title="' + escapeHtml(r.placeholder.query) + '" alt="' + escapeHtml(r.placeholder.query) + '" class="eva-inline-img"' + genLabel + '>';
        if (r.generated) {
          imgTag = '<div class="eva-generated-wrap">' + imgTag + '<span class="eva-generated-badge">AI Generated</span></div>';
        }
        text = text.replace(r.placeholder.full, imgTag);
        surfacedAssets.push({ url: r.url, caption: r.placeholder.query, generated: r.generated });
      } else {
        // Replace with a styled placeholder showing what was requested
        text = text.replace(r.placeholder.full, '[🖼️ ' + r.placeholder.query + ']');
      }
    });

    // If we successfully generated/found images, strip common AI disclaimers
    var anySuccess = results.some(function(r) { return r.url; });
    if (anySuccess && _lastUserAskedGenerate) {
      // Remove lines where the AI says it can't generate/create images
      text = text.replace(/I\s+(cannot|can't|can not|am unable to|don't have the ability to)\s+(generate|create|produce|make|draw|render)\s+(images?|pictures?|photos?|illustrations?|artwork)[^.]*\./gi, '');
      text = text.replace(/I\s+(can only|only)\s+describe[^.]*\./gi, '');
      text = text.replace(/\n{3,}/g, '\n\n'); // clean up extra blank lines
    }

    // Tokenize generated image wrappers and standalone <img> tags before markdown
    var imgFragments = [];
    // Tokenize cog-action artifact blocks (download links, ok/err markers) so
    // the markdown renderer doesn't escape their HTML into plain text.
    text = text.replace(/<div class="cog-action-(?:file|ok|err)">[\s\S]*?<\/div>/g, function(m) {
      imgFragments.push(m);
      return '\u0000IMG' + (imgFragments.length - 1) + '\u0000';
    });
    text = text.replace(/<div class="eva-generated-wrap">[\s\S]*?<\/div>/g, function(m) {
      imgFragments.push(m);
      return '\u0000IMG' + (imgFragments.length - 1) + '\u0000';
    });
    text = text.replace(/<img[^>]*>/g, function(m) {
      imgFragments.push(m);
      return '\u0000IMG' + (imgFragments.length - 1) + '\u0000';
    });

    // Render markdown
    var html = (typeof renderMarkdown === 'function') ? renderMarkdown(text) : text;

    // Restore <img> tags
    html = html.replace(/\u0000IMG(\d+)\u0000/g, function(m, idx) {
      return imgFragments[Number(idx)] || m;
    });

    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + html + '</div></div>';
  } else {
    // No images or no search keys — just render markdown
    // Still need to protect cog-action artifact HTML from being escaped.
    var actFragments = [];
    text = text.replace(/<div class="cog-action-(?:file|ok|err)">[\s\S]*?<\/div>/g, function(m) {
      actFragments.push(m);
      return '\u0000ACT' + (actFragments.length - 1) + '\u0000';
    });
    var html2 = (typeof renderMarkdown === 'function') ? renderMarkdown(text) : text;
    html2 = html2.replace(/\u0000ACT(\d+)\u0000/g, function(m, idx) {
      return actFragments[Number(idx)] || m;
    });
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="eva">Eva:</span> <div class="md">' + html2 + '</div></div>';
  }

  appendArtifactLinks();

  if (!responseIsCurrent()) return false;

  // Auto-open artifact files so the user doesn't have to click.
  if (artifactNames.length) {
    var bridgeUrl = getSafeBridgeBaseUrl();
    artifactNames.forEach(function(filename) {
      var openUrl = bridgeUrl + '/v1/files/' + encodeURIComponent(filename) + '?open=1';
      fetch(openUrl).then(function(res) {
        if (responseIsCurrent() && res.ok) console.log('[Artifact] Auto-opened:', filename);
      }).catch(function() {});
    });
  }

  txtOutput.scrollTop = txtOutput.scrollHeight;

  // In voice/visual mode the chat is hidden behind the orb overlay, so surface
  // any images Eva resolved into the voice view's asset window.
  if (typeof _vv !== 'undefined' && _vv.open && surfacedAssets.length) {
    _vvSurfaceAssets(surfacedAssets);
  }

  // Launch the visual browser agent if Eva requested it.
  if (browserLaunch && typeof EvaBrowser !== 'undefined' && EvaBrowser && typeof EvaBrowser.launch === 'function') {
    var browserComplete = function(status, endpoint, title) {
      if (!responseIsCurrent()) return;
      _evaAgentFeedback(status, endpoint, title, capturedEnvelope);
    };
    var browserConfirm = function(question, needsText, status) {
      if (!responseIsCurrent()) return;
      _evaAgentConfirmAsk(question, needsText, status, capturedEnvelope);
    };
    var browserProgress = function(subgoal, status) {
      if (!responseIsCurrent()) return;
      _evaAgentProgress(subgoal, status, capturedEnvelope);
    };
    EvaBrowser.launch(browserLaunch.goal, {
      start_url: browserLaunch.start_url,
      vision_model: browserLaunch.vision_model,
      max_steps: browserLaunch.max_steps,
      postcondition: browserLaunch.postcondition,
      onComplete: browserComplete,
      onConfirm: browserConfirm,
      onProgress: browserProgress
    });
  }

  // Launch the desktop ("computer use") agent if Eva requested it.
  if (desktopLaunch && typeof EvaDesktop !== 'undefined' && EvaDesktop && typeof EvaDesktop.launch === 'function') {
    var desktopComplete = function(status, endpoint, title) {
      if (!responseIsCurrent()) return;
      _evaAgentFeedback(status, endpoint, title, capturedEnvelope);
    };
    var desktopConfirm = function(question, needsText, status) {
      if (!responseIsCurrent()) return;
      _evaAgentConfirmAsk(question, needsText, status, capturedEnvelope);
    };
    var desktopProgress = function(subgoal, status) {
      if (!responseIsCurrent()) return;
      _evaAgentProgress(subgoal, status, capturedEnvelope);
    };
    EvaDesktop.launch(desktopLaunch.goal, {
      vision_model: desktopLaunch.vision_model,
      max_steps: desktopLaunch.max_steps,
      postcondition: desktopLaunch.postcondition,
      onComplete: desktopComplete,
      onConfirm: desktopConfirm,
      onProgress: desktopProgress
    });
  }

  // Look through the webcam if Eva requested it (Eva's eyes).
  if (cameraLook && typeof EvaCamera !== 'undefined' && EvaCamera && typeof EvaCamera.look === 'function') {
    EvaCamera.look(cameraLook.question).then(function (desc) {
      if (!responseIsCurrent()) return;
      _evaCameraLookResult(desc || 'I could not make out anything.');
    }).catch(function (err) {
      if (!responseIsCurrent()) return;
      _evaCameraLookResult('I tried to look but ' + ((err && err.message) ? err.message : 'something went wrong') + '.');
    });
  }

  // Auto-save session after each response
  if (responseIsCurrent() && typeof saveCurrentSession === 'function') saveCurrentSession();
  return responseIsCurrent();
}

/**
 * Extract the key subject from a verbose image description.
 * "GitHub's Octocat mascot - a friendly cartoon cat..." → "GitHub Octocat mascot"
 */
function _extractImageSubject(rawDesc) {
  if (!rawDesc) return '';
  var desc = rawDesc;

  // Cut at first " - " or " — " or ", " comma phrase
  var dashIdx = desc.search(/\s[-–—]\s/);
  if (dashIdx > 3) desc = desc.substring(0, dashIdx);

  // Cut at first comma if still long (keep just the subject noun phrase)
  if (desc.length > 40) {
    var commaIdx = desc.indexOf(',');
    if (commaIdx > 3) desc = desc.substring(0, commaIdx);
  }

  // Cut at first period
  if (desc.length > 40) {
    var dotIdx = desc.indexOf('.');
    if (dotIdx > 3) desc = desc.substring(0, dotIdx);
  }

  // 1. Find proper nouns (capitalized words like "Octocat", "GitHub")
  var properNouns = desc.match(/\b[A-Z][a-zA-Z]+\b/g) || [];
  var ignoreCapitalized = new Set(['Image', 'Picture', 'Photo', 'The', 'An', 'This', 'Here', 'Its', 'Each', 'Very', 'Some', 'With', 'And']);
  properNouns = properNouns.filter(function(w) { return !ignoreCapitalized.has(w); });

  if (properNouns.length > 0) {
    return properNouns.slice(0, 4).join(' ');
  }

  // 2. Strip filler — keep nouns (which come early in the description)
  desc = desc
    .replace(/^(an?|the|image of|picture of|photo of|showing|depicting|illustration of)\s+/gi, '')
    .replace(/\b(friendly|cartoon|cartoonish|cute|classic|iconic|simple|round|large|small|playful|beloved|stylized|detailed|colorful|whimsical|famous|popular|vibrant|modern|typical|standard|featuring|with|that|has|and|or|its|soft|warm|bright|relaxed|graceful|sunny|patterned)\b\s*/gi, '')
    .replace(/[''\u2019]s\b/g, '')
    .replace(/[,;]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  // Take FIRST 2-3 meaningful words (the subject noun is at the beginning)
  var words = desc.split(/\s+/).filter(function(w) { return w.length > 2; });
  if (words.length > 3) {
    desc = words.slice(0, 3).join(' ');
  }

  return desc || rawDesc.substring(0, 30);
}

/**
 * Search for an image using Wikimedia Commons (free, no API key needed).
 */
async function _searchImage(query) {
  if (!query) return null;

  var cleanQuery = query.trim();
  if (!cleanQuery) return null;

  // Try progressively simpler queries
  var queries = [cleanQuery];
  var words = cleanQuery.split(/\s+/);
  if (words.length > 2) queries.push(words.slice(0, 2).join(' '));
  if (words.length > 1) queries.push(words[words.length - 1]); // try just the last word (often the noun)

  for (var qi = 0; qi < queries.length; qi++) {
    var q = queries[qi];
    try {
      var wUrl = 'https://commons.wikimedia.org/w/api.php?' +
        'action=query&list=search&srnamespace=6' +
        '&srsearch=' + encodeURIComponent(q) +
        '&srlimit=5&format=json&origin=*';

      var wResp = await fetch(wUrl);
      if (wResp.ok) {
        var wData = await wResp.json();
        var results = (wData.query && wData.query.search) || [];
        if (results.length > 0) {
          // Get the actual image URL from the file title
          var fileTitle = results[0].title;
          var imgUrl = 'https://commons.wikimedia.org/w/api.php?' +
            'action=query&titles=' + encodeURIComponent(fileTitle) +
            '&prop=imageinfo&iiprop=url&iiurlwidth=400&format=json&origin=*';

          var imgResp = await fetch(imgUrl);
          if (imgResp.ok) {
            var imgData = await imgResp.json();
            var pages = imgData.query && imgData.query.pages;
            if (pages) {
              var pageId = Object.keys(pages)[0];
              var info = pages[pageId].imageinfo;
              if (info && info[0]) {
                return info[0].thumburl || info[0].url;
              }
            }
          }
        } else if (qi < queries.length - 1) {
          // Try simpler query
        }
      }
    } catch (e) {
      console.warn('Wikimedia search error:', e.message);
    }
  }

  return null;
}

// --- Image Lightbox ---
document.addEventListener('DOMContentLoaded', function() {
  var lightbox = document.getElementById('evaLightbox');
  var lightboxImg = document.getElementById('evaLightboxImg');
  var lightboxClose = lightbox ? lightbox.querySelector('.eva-lightbox-close') : null;

  // Click on any inline image to expand
  document.addEventListener('click', function(e) {
    var img = e.target.closest('.eva-inline-img');
    if (img && lightbox && lightboxImg) {
      lightboxImg.src = img.src;
      lightboxImg.alt = img.alt || 'Expanded image';
      lightbox.classList.add('open');
      e.preventDefault();
    }
  });

  // Close lightbox
  if (lightbox) {
    lightbox.addEventListener('click', function(e) {
      if (e.target === lightbox || e.target === lightboxClose) {
        lightbox.classList.remove('open');
      }
    });
  }

  // Escape key closes lightbox
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && lightbox && lightbox.classList.contains('open')) {
      lightbox.classList.remove('open');
    }
  });
});

// Capture Shift + Enter Keys for new line
function shiftBreak() {
document.querySelector("#txtMsg").addEventListener("keydown", function(event) {
  if (event.shiftKey && event.keyCode === 13) {
    // Use the browser's native line-break command so contenteditable
    // gets the proper trailing-br anchor. The previous manual <br>
    // insert required two presses to visually break the line because
    // a single trailing <br> at end-of-text is not rendered.
    event.preventDefault();
    try {
      if (document.execCommand && document.execCommand('insertLineBreak')) {
        return;
      }
    } catch (_) {}
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    range.deleteContents();
    var br = document.createElement("br");
    range.insertNode(br);
    // Anchor br: ensures the cursor falls on a visibly new line.
    var anchor = document.createElement("br");
    range.setStartAfter(br);
    range.insertNode(anchor);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  }
});

    // Capture Enter Key to Send Message and Backspace to reset position
    document.querySelector("#txtMsg").addEventListener("keydown", function(event) {
      if (event.keyCode === 13 && !event.shiftKey) {
        document.querySelector("#btnSend").click();
        event.preventDefault();
        var backspace = new KeyboardEvent("keydown", {
          bubbles: true,
          cancelable: true,
          keyCode: 8
        });
        document.querySelector("#txtMsg").dispatchEvent(backspace);
      }
    });
}

// Clear Messages for Clear Memory Button
function resetTransientConversationState() {
  if (typeof invalidateSessionLoads === 'function') invalidateSessionLoads();
    lastResponse = "";
    userMasterResponse = "";
    aiMasterResponse = "";
    masterOutput = "";
    storageAssistant = "";
    retryCount = 0;
    if (typeof window !== 'undefined') {
      window._evaSessionId = '';
      window._evaTurnId = '';
    }
    if (typeof _resetAgentInteractionState === 'function') {
      _resetAgentInteractionState();
    }
}

function clearMessages() {
    // Preserve auth keys, settings, and session data across clear
    var keysToKeep = [];
    for (var i = 0; i < localStorage.length; i++) {
      var key = localStorage.key(i);
      if (key && (key.indexOf('auth_') === 0 || key === 'theme' || key === 'systemPrompt'
          || key === 'lcars_collapsed' || key === 'acp_bridge_url'
          || key === 'aig_lmstudio_base_url' || key === 'aig_lmstudio_model'
          || key === 'eva_sessions' || key.indexOf('session_') === 0)) {
        keysToKeep.push({ k: key, v: localStorage.getItem(key) });
      }
    }
    localStorage.clear();
    keysToKeep.forEach(function(item) { localStorage.setItem(item.k, item.v); });
    // Start a fresh session (don't carry old active id)
    localStorage.removeItem('eva_active_session');
    resetTransientConversationState();
    if (typeof resetEnvelopeSession === 'function') resetEnvelopeSession();
    document.getElementById("txtOutput").innerHTML = "\n" + "		MEMORY CLEARED";
}

// Restore the Eva welcome MOTD into #txtOutput after clearing
function restoreEvaWelcome() {
  var out = document.getElementById('txtOutput');
  if (!out) return;
  var theme = (localStorage.getItem('theme') || 'eva');
  if (theme !== 'eva' && theme.indexOf('eva-') !== 0) return;
  out.innerHTML = '<div id="evaWelcome" class="eva-welcome">'
    + '<img src="core/img/thumb-125.jpeg" alt="Eva" class="eva-welcome-avatar">'
    + '<h2 class="eva-welcome-title">Hello! I\'m <span class="eva-highlight">Eva</span></h2>'
    + '<p class="eva-welcome-subtitle">Your AI assistant. Ask me anything or choose a suggestion to get started.</p>'
    + '<div class="eva-suggestions">'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Explain a complex topic in simple terms"><span class="eva-sug-icon">&#x1F9E0;</span><div><strong>Explain a complex topic</strong><br><span class="eva-sug-sub">in simple terms</span></div></button>'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Help me write code in any language"><span class="eva-sug-icon">&lt;/&gt;</span><div><strong>Help me write code</strong><br><span class="eva-sug-sub">in any language</span></div></button>'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Brainstorm ideas for a project"><span class="eva-sug-icon">&#x1F4A1;</span><div><strong>Brainstorm ideas</strong><br><span class="eva-sug-sub">for a project</span></div></button>'
    + '<button class="eva-suggestion" onclick="evaSuggestionClick(this)" data-prompt="Review my text and improve it"><span class="eva-sug-icon">&#x270F;&#xFE0F;</span><div><strong>Review my text</strong><br><span class="eva-sug-sub">and improve it</span></div></button>'
    + '</div></div>';
}

// Text-to-Speech (voice recognition moved to voice.js)
