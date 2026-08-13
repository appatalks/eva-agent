// Alerts Settings workflow: watch rules, delivery limits, and CRUD lifecycle.
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

function updateAlertParamFields() {
  var type = (document.getElementById('alertType') || {}).value || 'keyword_watch';
  var topicWrap = document.getElementById('alertParamTopicWrap');
  var topicLabel = document.getElementById('alertParamTopicLabel');
  var topicInput = document.getElementById('alertParamTopic');
  var condWrap = document.getElementById('alertParamConditionWrap');
  var showTopic = true, showCond = false, label = 'Topic to watch', placeholder = '';
  switch (type) {
    case 'keyword_watch': label = 'Topic to watch'; placeholder = 'e.g. new OpenAI model releases'; break;
    case 'research_question': label = 'Question to track'; placeholder = 'e.g. has the Fed changed interest rates?'; break;
    case 'sec_filing': label = 'Ticker symbols (comma separated)'; placeholder = 'e.g. AAPL, MSFT'; break;
    case 'weather': label = 'Location'; placeholder = 'e.g. Seattle, WA'; showCond = true; break;
    case 'space_weather': showTopic = false; break;
  }
  if (topicWrap) topicWrap.style.display = showTopic ? '' : 'none';
  if (topicLabel) topicLabel.textContent = label;
  if (topicInput) topicInput.placeholder = placeholder;
  if (condWrap) condWrap.style.display = showCond ? '' : 'none';
}

function buildAlertParams(type, topicValue, conditionValue) {
  switch (type) {
    case 'keyword_watch': return { topic: topicValue };
    case 'research_question': return { question: topicValue };
    case 'sec_filing': return { symbols: topicValue };
    case 'weather': return { location: topicValue, condition: conditionValue };
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
    var toggleButton = document.createElement('button');
    toggleButton.type = 'button';
    toggleButton.className = 'auth-toggle background-inline-button';
    toggleButton.textContent = rule.enabled ? 'Pause' : 'Resume';
    toggleButton.addEventListener('click', function() { toggleAlert(rule.id); });
    actions.appendChild(toggleButton);
    var deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'auth-toggle';
    deleteButton.textContent = 'Delete';
    deleteButton.addEventListener('click', function() { deleteAlert(rule.id); });
    actions.appendChild(deleteButton);
    head.appendChild(actions);
    row.appendChild(head);
    var meta = document.createElement('div');
    meta.className = 'background-meta';
    var params = rule.params || {};
    var detail = params.topic || params.question || params.location || (params.symbols ? params.symbols.join(', ') : '') || alertTypeLabel(rule.type);
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
    var settings = _alertsState.settings;
    var quietStart = document.getElementById('alertQuietStart');
    var quietEnd = document.getElementById('alertQuietEnd');
    var maxPerHour = document.getElementById('alertMaxPerHour');
    if (quietStart && typeof settings.quiet_hours_start === 'number') quietStart.value = settings.quiet_hours_start;
    if (quietEnd && typeof settings.quiet_hours_end === 'number') quietEnd.value = settings.quiet_hours_end;
    if (maxPerHour && typeof settings.max_per_hour === 'number') maxPerHour.value = settings.max_per_hour;
  } catch (error) {
    var listEl = document.getElementById('alertsList');
    if (listEl) listEl.innerHTML = '<div class="auth-note">' + escapeHtml(error.message || 'Alerts unavailable.') + '</div>';
  }
}

function readAlertForm() {
  var type = (document.getElementById('alertType') || {}).value || 'keyword_watch';
  var label = (document.getElementById('alertLabel') || {}).value || '';
  var topic = (document.getElementById('alertParamTopic') || {}).value || '';
  var condition = (document.getElementById('alertParamCondition') || {}).value || '';
  var cooldownHours = parseInt((document.getElementById('alertCooldown') || {}).value, 10);
  if (!Number.isInteger(cooldownHours) || cooldownHours < 1) cooldownHours = 24;
  var channels = [];
  if ((document.getElementById('alertChannelChat') || {}).checked) channels.push('chat');
  if ((document.getElementById('alertChannelVoice') || {}).checked) channels.push('voice');
  if (!channels.length) channels.push('chat');
  return {
    type: type,
    label: label.trim() || alertTypeLabel(type),
    params: buildAlertParams(type, topic.trim(), condition.trim()),
    cooldown_min: cooldownHours * 60,
    channels: channels,
    enabled: (document.getElementById('alertEnabled') || {}).checked !== false
  };
}

function alertValidationMessage(rule) {
  var params = rule.params || {};
  if (rule.type === 'keyword_watch' && !params.topic) return 'Topic to watch is required.';
  if (rule.type === 'research_question' && !params.question) return 'Question to track is required.';
  if (rule.type === 'sec_filing' && !params.symbols) return 'At least one ticker symbol is required.';
  if (rule.type === 'weather' && !params.location) return 'Location is required.';
  return '';
}

async function saveAlert() {
  var rule = readAlertForm();
  var validationMessage = alertValidationMessage(rule);
  if (validationMessage) {
    setStatus('error', validationMessage);
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
  var rule = _alertsState.alerts.filter(function(item) { return item.id === id; })[0];
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

function readAlertSettings() {
  return {
    quiet_hours_start: parseInt((document.getElementById('alertQuietStart') || {}).value, 10),
    quiet_hours_end: parseInt((document.getElementById('alertQuietEnd') || {}).value, 10),
    max_per_hour: parseInt((document.getElementById('alertMaxPerHour') || {}).value, 10)
  };
}

async function saveAlertSettings() {
  try {
    await backgroundBridgeRequest('/v1/alerts/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(readAlertSettings())
    });
    await loadAlerts();
    setStatus('info', 'Notification limits saved.');
  } catch (error) {
    setStatus('error', error.message || 'Could not save limits.');
  }
}

function clearAlertForm() {
  ['alertLabel', 'alertParamTopic', 'alertParamCondition'].forEach(function(id) {
    var element = document.getElementById(id);
    if (element) element.value = '';
  });
  var cooldown = document.getElementById('alertCooldown');
  if (cooldown) cooldown.value = 24;
  var enabled = document.getElementById('alertEnabled');
  if (enabled) enabled.checked = true;
}

function initAlerts() {
  var typeSelect = document.getElementById('alertType');
  var saveButton = document.getElementById('alertSaveButton');
  var clearButton = document.getElementById('alertClearButton');
  var settingsButton = document.getElementById('alertSettingsSaveButton');
  if (typeSelect) typeSelect.addEventListener('change', updateAlertParamFields);
  if (saveButton) saveButton.addEventListener('click', saveAlert);
  if (clearButton) clearButton.addEventListener('click', clearAlertForm);
  if (settingsButton) settingsButton.addEventListener('click', saveAlertSettings);
  updateAlertParamFields();
  loadAlerts();
}