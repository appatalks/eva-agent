// Goals settings workflow: bridge lifecycle, form validation, and rendering.
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
  if (text && !quiet) setStatus(type === 'error' ? 'error' : 'info', text);
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
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) options.signal = AbortSignal.timeout(3000);
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
  var body = { title: titleValue, description: descriptionValue, category: category ? category.value : 'relational', priority: priorityValue, relatedTopics: '' };
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
    await goalsBridgeRequest(path, { method: isEdit ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData.body) });
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
    await goalsBridgeRequest('/v1/goals/' + encodeURIComponent(goalId), { method: 'DELETE' });
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