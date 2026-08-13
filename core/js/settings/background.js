// Background Settings workflow: status, jobs, proposals, activity, and controls.
var _backgroundState = { status: null, proposals: [], activity: [], loading: false, error: '' };

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
      if (Object.prototype.hasOwnProperty.call(status.jobs, jobType)) input.checked = !!status.jobs[jobType];
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
    emptyEl.textContent = 'No pending proposals.';
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
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) options.signal = AbortSignal.timeout(3000);
    _backgroundState.status = await backgroundBridgeRequest('/v1/background/status', options);
    try {
      var proposalData = await backgroundBridgeRequest('/v1/background/proposals?status=pending', options);
      var activityData = await backgroundBridgeRequest('/v1/background/activity', options);
      _backgroundState.proposals = Array.isArray(proposalData.proposals) ? proposalData.proposals : [];
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

function readBackgroundControls(runNow) {
  var enabledEl = document.getElementById('backgroundEnabled');
  var intervalEl = document.getElementById('backgroundIntervalSeconds');
  var intervalValue = intervalEl ? parseInt(intervalEl.value, 10) : 7200;
  if (!Number.isInteger(intervalValue) || intervalValue < 900 || intervalValue > 86400) {
    return { error: 'Interval must be between 900 and 86400 seconds.' };
  }
  var jobs = {};
  var jobInputs = document.querySelectorAll('#backgroundJobs input[data-job]');
  Array.prototype.forEach.call(jobInputs, function(input) {
    jobs[input.getAttribute('data-job')] = !!input.checked;
  });
  return {
    body: {
      enabled: enabledEl ? !!enabledEl.checked : true,
      intervalSeconds: intervalValue,
      jobs: jobs,
      runNow: !!runNow
    }
  };
}

async function saveBackgroundControls(runNow) {
  var controls = readBackgroundControls(runNow);
  var saveButton = document.getElementById('backgroundSaveButton');
  var runButton = document.getElementById('backgroundRunNowButton');
  if (controls.error) {
    _backgroundState.error = controls.error;
    renderBackgroundStatus();
    setStatus('error', _backgroundState.error);
    return;
  }
  if (saveButton) saveButton.disabled = true;
  if (runButton) runButton.disabled = true;
  try {
    var data = await backgroundBridgeRequest('/v1/background/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(controls.body)
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