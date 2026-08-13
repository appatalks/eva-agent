// Cron Settings workflow: recurring task validation, bridge requests, and list rendering.
function _escHtml(value) {
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(value || ''));
  return div.innerHTML;
}

async function cronRefresh() {
  var listEl = document.getElementById('cronList');
  var statusEl = document.getElementById('cronStatus');
  try {
    var bridgeUrl = await detectACPBridge();
    var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron');
    var data = await response.json();
    if (!response.ok) {
      if (statusEl) statusEl.textContent = 'Error: ' + (data.error ? data.error.message : 'unknown');
      return;
    }
    if (statusEl) statusEl.textContent = data.count + ' task(s)';
    if (!listEl) return;
    var tasks = data.tasks || [];
    if (!tasks.length) {
      listEl.innerHTML = '<p class="auth-note">No cron tasks. Add one above.</p>';
      return;
    }
    var html = '';
    tasks.forEach(function(task) {
      var enabled = task.enabled !== false;
      html += '<div class="background-item" style="margin-bottom:10px;padding:8px;border:1px solid rgba(127,127,127,0.2);border-radius:6px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center">';
      html += '<strong>' + _escHtml(task.label) + '</strong>';
      html += '<span style="font-size:11px;opacity:0.7">' + _escHtml(task.schedule) + '</span>';
      html += '</div>';
      html += '<p style="margin:4px 0;font-size:12px;opacity:0.8">' + _escHtml(task.prompt).substring(0, 200) + '</p>';
      html += '<div style="font-size:11px;opacity:0.6">';
      if (task.next_run) html += 'Next: ' + task.next_run.substring(0, 16).replace('T', ' ') + ' UTC';
      if (task.last_run) html += ' | Last: ' + task.last_run.substring(0, 16).replace('T', ' ') + ' UTC';
      html += '</div>';
      html += '<div style="margin-top:6px;display:flex;gap:6px">';
      html += '<button class="auth-toggle" style="font-size:11px;padding:2px 8px" onclick="cronToggle(\'' + task.id + '\',' + !enabled + ')">' + (enabled ? 'Disable' : 'Enable') + '</button>';
      html += '<button class="auth-toggle" style="font-size:11px;padding:2px 8px;color:#c44" onclick="cronDelete(\'' + task.id + '\')">Delete</button>';
      html += '</div></div>';
    });
    listEl.innerHTML = html;
  } catch (error) {
    if (statusEl) statusEl.textContent = 'Failed: ' + error.message;
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
    var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: label.trim(), schedule: schedule.trim(), prompt: prompt.trim() })
    });
    var data = await response.json();
    if (!response.ok) {
      if (statusEl) statusEl.textContent = 'Error: ' + (data.error ? data.error.message : 'unknown');
      return;
    }
    if (statusEl) statusEl.textContent = 'Created: ' + (data.task ? data.task.label : '');
    ['cronLabel', 'cronSchedule', 'cronPrompt'].forEach(function(id) {
      var field = document.getElementById(id);
      if (field) field.value = '';
    });
    cronRefresh();
  } catch (error) {
    if (statusEl) statusEl.textContent = 'Failed: ' + error.message;
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
  } catch (_) {}
}

async function cronDelete(taskId) {
  try {
    var bridgeUrl = await detectACPBridge();
    await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/cron/' + encodeURIComponent(taskId), { method: 'DELETE' });
    cronRefresh();
  } catch (_) {}
}