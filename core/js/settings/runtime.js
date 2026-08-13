// Settings runtime controls: data retrieval mode and local bridge diagnostics.
function settingsBridgeUrl() {
  var bridgeUrl = typeof getACPBridgeUrl === 'function' ? getACPBridgeUrl() : 'http://localhost:8888';
  return bridgeUrl.replace(/\/+$/, '');
}

async function diagnosticsBridgeUrl() {
  if (typeof detectACPBridge === 'function') return await detectACPBridge();
  return settingsBridgeUrl();
}

function switchDataMode(mode) {
  var statusEl = document.getElementById('dataModeStatus');
  if (statusEl) statusEl.textContent = 'Switching to ' + mode + '...';
  fetch(settingsBridgeUrl() + '/v1/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: mode }),
    signal: AbortSignal.timeout(30000)
  })
  .then(function(response) { return response.json(); })
  .then(function(data) {
    if (data.error) {
      if (statusEl) statusEl.textContent = 'Error: ' + (data.error.message || data.error);
      return;
    }
    var select = document.getElementById('selDataMode');
    if (select) select.value = data.mode;
    try { localStorage.setItem('evaDataMode', data.mode); } catch (_) {}
    if (!statusEl) return;
    statusEl.textContent = data.mode === 'local'
      ? 'Local mode active (' + (data.local_tools || 0) + ' MCP tools available)'
      : 'Cloud mode active (Copilot CLI)';
  })
  .catch(function(error) {
    if (statusEl) statusEl.textContent = 'Error: ' + error.message;
  });
}

function loadDataMode() {
  fetch(settingsBridgeUrl() + '/v1/mode', { signal: AbortSignal.timeout(3000) })
  .then(function(response) { return response.json(); })
  .then(function(data) {
    var select = document.getElementById('selDataMode');
    if (select) select.value = data.mode || 'cloud';
    try { localStorage.setItem('evaDataMode', data.mode || 'cloud'); } catch (_) {}
    var statusEl = document.getElementById('dataModeStatus');
    if (!statusEl) return;
    var parts = [];
    parts.push(data.cloud_available ? 'Cloud: available' : 'Cloud: unavailable');
    parts.push(data.local_available ? 'Local: ' + data.local_tools + ' tools' : 'Local: not started');
    statusEl.textContent = parts.join(' | ');
  })
  .catch(function() {});
}

function formatDoctorReport(data) {
  var lines = [];
  var readiness = data.readiness || {};
  var blockers = data.blockers || [];
  lines.push('=== Eva Diagnostics ===');
  lines.push('');
  lines.push('Readiness:');
  [
    ['Chat (ACP)', readiness.can_chat],
    ['Browser agent', readiness.can_browse],
    ['Desktop agent', readiness.can_desktop],
    ['Camera/vision', readiness.can_see],
    ['Memory (Kusto)', readiness.can_remember],
    ['Background loop', readiness.can_schedule],
    ['Cron tasks', readiness.can_cron]
  ].forEach(function(check) {
    lines.push('  ' + (check[1] ? '\u2705' : '\u274c') + ' ' + check[0]);
  });
  var subsystems = data.subsystems || {};
  if (subsystems.system) {
    lines.push('');
    lines.push('System: Python ' + (subsystems.system.python || '?') + ', Node ' + (subsystems.system.node || 'not found'));
    lines.push('Platform: ' + (subsystems.system.platform || '?') + ' (' + (subsystems.system.arch || '?') + ')');
  }
  if (subsystems.mcp) lines.push('MCP servers: ' + (subsystems.mcp.configured || []).join(', ') || 'none');
  if (subsystems.desktop_agent) {
    if (subsystems.desktop_agent.computer_use_linux_available) lines.push('computer-use-linux: installed');
    if (subsystems.desktop_agent.ydotool_available) lines.push('ydotool: available');
  }
  if (blockers.length) {
    lines.push('');
    lines.push('Blockers:');
    blockers.forEach(function(blocker) { lines.push('  - ' + blocker); });
  }
  return lines.join('\n');
}

async function runDoctor() {
  var button = document.getElementById('doctorButton');
  var report = document.getElementById('doctorReport');
  if (button) button.disabled = true;
  if (report) {
    report.style.display = 'block';
    report.textContent = 'Running diagnostics...';
  }
  try {
    var response = await fetch((await diagnosticsBridgeUrl()).replace(/\/+$/, '') + '/v1/doctor');
    var data = await response.json();
    if (!response.ok) {
      if (report) report.textContent = 'Error: ' + (data.error ? data.error.message : 'unknown');
      return;
    }
    if (report) report.textContent = formatDoctorReport(data);
  } catch (error) {
    if (report) report.textContent = 'Failed: ' + error.message + ' \u2014 Is the bridge running?';
  } finally {
    if (button) button.disabled = false;
  }
}