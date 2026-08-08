// ===========================================================================
// Eva Skills importer
// ---------------------------------------------------------------------------
// Import a skill from a variety of sources (paste, URL, GitHub, file upload),
// have Eva normalize ("Eva'rise") it into her schema via the bridge, review the
// draft, then save it to ADX. Saved skills are surfaced automatically at runtime
// by semantic match in the bridge's memory-context injection.
//
// Bridge endpoints used:
//   POST /v1/skills/evarise  -> { draft }
//   GET  /v1/skills          -> { skills: [...] }
//   POST /v1/skills          -> { skill }
//   PATCH  /v1/skills/<id>   -> { skill }   (enable/disable/edit)
//   DELETE /v1/skills/<id>   -> { skill }
//
// Bridge calls reuse backgroundBridgeRequest() from options.js (same bridge).
// ===========================================================================

var _skillsState = {
  skills: [],
  draft: null,
  editingId: null,
  query: '',
  statusFilter: 'all',
  sourceFilter: 'all',
  sort: 'updated'
};

function _skillsBridge(path, options) {
  if (typeof backgroundBridgeRequest === 'function') {
    return backgroundBridgeRequest(path, options);
  }
  return Promise.reject(new Error('Bridge unavailable'));
}

function _skillStatus(msg, isError) {
  var el = document.getElementById('skillImportStatus');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = isError ? '#d66' : '';
}

// Show only the input relevant to the selected source type.
function updateSkillSourceFields() {
  var type = (document.getElementById('skillSourceType') || {}).value || 'paste';
  var map = {
    paste: 'skillPasteWrap',
    url: 'skillUrlWrap',
    github: 'skillRepoWrap',
    file: 'skillFileWrap'
  };
  Object.keys(map).forEach(function (k) {
    var el = document.getElementById(map[k]);
    if (el) el.style.display = (k === type) ? '' : 'none';
  });
}

// Read an uploaded file's text client-side so the bridge only ever needs to
// handle pasted content for the file path (no server-side file access).
function _readSkillFile() {
  return new Promise(function (resolve, reject) {
    var input = document.getElementById('skillFileInput');
    var file = input && input.files && input.files[0];
    if (!file) { reject(new Error('No file selected')); return; }
    if (file.size > 200 * 1024) { reject(new Error('File is too large (max 200 KB)')); return; }
    var reader = new FileReader();
    reader.onload = function () { resolve({ content: String(reader.result || ''), filename: file.name }); };
    reader.onerror = function () { reject(new Error('Could not read file')); };
    reader.readAsText(file);
  });
}

async function evariseSkill() {
  var type = (document.getElementById('skillSourceType') || {}).value || 'paste';
  var btn = document.getElementById('skillEvariseButton');
  var payload = { source_type: type };
  try {
    if (type === 'paste') {
      payload.content = (document.getElementById('skillPasteInput') || {}).value || '';
      if (!payload.content.trim()) { _skillStatus('Paste some skill content first.', true); return; }
    } else if (type === 'url') {
      payload.url = (document.getElementById('skillUrlInput') || {}).value || '';
      if (!payload.url.trim()) { _skillStatus('Enter a URL first.', true); return; }
    } else if (type === 'github') {
      payload.repo = (document.getElementById('skillRepoInput') || {}).value || '';
      if (!payload.repo.trim()) { _skillStatus('Enter a GitHub reference first.', true); return; }
    } else if (type === 'file') {
      var f = await _readSkillFile();
      payload.source_type = 'file';
      payload.content = f.content;
      payload.filename = f.filename;
    }
  } catch (e) {
    _skillStatus(e.message || 'Could not read source.', true);
    return;
  }

  if (btn) btn.disabled = true;
  _skillStatus("Eva is reading and normalizing the skill...");
  try {
    var data = await _skillsBridge('/v1/skills/evarise', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!data || !data.draft) { throw new Error('No draft returned'); }
    _skillsState.draft = data.draft;
    _populateSkillDraft(data.draft);
    _skillStatus("Eva'rised. Review and save below.");
  } catch (error) {
    _skillStatus(error.message || "Eva'rise failed.", true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _populateSkillDraft(draft) {
  var wrap = document.getElementById('skillDraft');
  if (wrap) wrap.style.display = '';
  var set = function (id, v) { var el = document.getElementById(id); if (el) el.value = v || ''; };
  set('skillDraftName', draft.name);
  set('skillDraftDescription', draft.description);
  set('skillDraftInstructions', draft.instructions);
  set('skillDraftTools', draft.tools);
  set('skillDraftTags', draft.tags);
}

function cancelSkillDraft() {
  _skillsState.draft = null;
  _skillsState.editingId = null;
  var wrap = document.getElementById('skillDraft');
  if (wrap) wrap.style.display = 'none';
  var saveButton = document.getElementById('skillSaveButton');
  if (saveButton) saveButton.textContent = 'Save skill';
}

async function saveSkill() {
  var get = function (id) { var el = document.getElementById(id); return el ? el.value.trim() : ''; };
  var skill = {
    name: get('skillDraftName'),
    description: get('skillDraftDescription'),
    instructions: get('skillDraftInstructions'),
    tools: get('skillDraftTools'),
    tags: get('skillDraftTags'),
    source: (_skillsState.draft && _skillsState.draft.source) || 'paste'
  };
  if (!skill.name) { _skillStatus('Give the skill a name.', true); return; }
  if (!skill.instructions) { _skillStatus('The skill needs instructions.', true); return; }
  var btn = document.getElementById('skillSaveButton');
  if (btn) btn.disabled = true;
  try {
    var editingId = _skillsState.editingId;
    await _skillsBridge(editingId ? '/v1/skills/' + encodeURIComponent(editingId) : '/v1/skills', {
      method: editingId ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(skill)
    });
    cancelSkillDraft();
    clearSkillImport();
    await loadSkills();
    var message = editingId ? 'Skill updated.' : 'Skill saved.';
    if (typeof setStatus === 'function') setStatus('info', message);
    _skillStatus(message);
  } catch (error) {
    _skillStatus(error.message || 'Could not save skill.', true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function clearSkillImport() {
  ['skillPasteInput', 'skillUrlInput', 'skillRepoInput'].forEach(function (id) {
    var el = document.getElementById(id); if (el) el.value = '';
  });
  var f = document.getElementById('skillFileInput'); if (f) f.value = '';
}

function _skillField(row, primary, alt) {
  if (!row) return '';
  if (row[primary] !== undefined && row[primary] !== null) return row[primary];
  if (alt && row[alt] !== undefined && row[alt] !== null) return row[alt];
  return '';
}

function _skillCsv(value) {
  return String(value || '').split(',').map(function(item) { return item.trim(); }).filter(Boolean);
}

function _skillUpdatedAt(skill) {
  var value = _skillField(skill, 'UpdatedAt', 'updatedAt') || _skillField(skill, 'CreatedAt', 'createdAt');
  var timestamp = Date.parse(value || '');
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function _skillSourceKind(value) {
  var source = String(value || 'unknown').trim().toLowerCase();
  var known = ['paste', 'url', 'github', 'file', 'auto-learned', 'edited'];
  for (var i = 0; i < known.length; i++) {
    if (source === known[i] || source.indexOf(known[i] + ':') === 0) return known[i];
  }
  return 'unknown';
}

function _filteredSkills() {
  var query = _skillsState.query.toLowerCase();
  var skills = _skillsState.skills.filter(function(skill) {
    var status = String(_skillField(skill, 'Status', 'status') || 'active').toLowerCase();
    var sourceValue = String(_skillField(skill, 'Source', 'source') || 'unknown');
    var source = _skillSourceKind(sourceValue);
    if (_skillsState.statusFilter !== 'all' && status !== _skillsState.statusFilter) return false;
    if (_skillsState.sourceFilter !== 'all' && source !== _skillsState.sourceFilter) return false;
    if (!query) return true;
    var searchable = [
      _skillField(skill, 'Name', 'name'),
      _skillField(skill, 'Description', 'description'),
      _skillField(skill, 'Instructions', 'instructions'),
      _skillField(skill, 'Tools', 'tools'),
      _skillField(skill, 'Tags', 'tags'),
      sourceValue,
      status
    ].join(' ').toLowerCase();
    return searchable.indexOf(query) !== -1;
  });
  skills.sort(function(left, right) {
    if (_skillsState.sort === 'name') {
      return String(_skillField(left, 'Name', 'name')).localeCompare(String(_skillField(right, 'Name', 'name')));
    }
    if (_skillsState.sort === 'status') {
      var leftStatus = String(_skillField(left, 'Status', 'status') || 'active');
      var rightStatus = String(_skillField(right, 'Status', 'status') || 'active');
      return leftStatus.localeCompare(rightStatus) || String(_skillField(left, 'Name', 'name')).localeCompare(String(_skillField(right, 'Name', 'name')));
    }
    return _skillUpdatedAt(right) - _skillUpdatedAt(left);
  });
  return skills;
}

function _updateSkillSummary(visibleCount) {
  var summary = document.getElementById('skillsViewSummary');
  if (!summary) return;
  var active = _skillsState.skills.filter(function(skill) {
    return String(_skillField(skill, 'Status', 'status') || 'active') === 'active';
  }).length;
  summary.textContent = visibleCount + ' shown | ' + active + ' active | ' + _skillsState.skills.length + ' total';
}

function editSkill(skill) {
  var id = String(_skillField(skill, 'SkillId', 'skillId') || '');
  if (!id) return;
  _skillsState.editingId = id;
  _skillsState.draft = {
    name: String(_skillField(skill, 'Name', 'name') || ''),
    description: String(_skillField(skill, 'Description', 'description') || ''),
    instructions: String(_skillField(skill, 'Instructions', 'instructions') || ''),
    tools: String(_skillField(skill, 'Tools', 'tools') || ''),
    tags: String(_skillField(skill, 'Tags', 'tags') || ''),
    source: String(_skillField(skill, 'Source', 'source') || 'edited')
  };
  _populateSkillDraft(_skillsState.draft);
  var saveButton = document.getElementById('skillSaveButton');
  if (saveButton) saveButton.textContent = 'Update skill';
  _skillStatus('Editing ' + (_skillsState.draft.name || 'skill') + '. Save to reimport the updated definition.');
  var draft = document.getElementById('skillDraft');
  if (draft && draft.scrollIntoView) draft.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderSkillsList() {
  var listEl = document.getElementById('skillsList');
  if (!listEl) return;
  listEl.innerHTML = '';
  var skills = _filteredSkills();
  _updateSkillSummary(skills.length);
  if (!skills.length) {
    var empty = document.createElement('div');
    empty.className = 'skills-view-empty';
    empty.textContent = _skillsState.skills.length ? 'No skills match these filters.' : 'No skills yet. Import one from the editor.';
    listEl.appendChild(empty);
    return;
  }
  skills.forEach(function (sk) {
    var id = String(_skillField(sk, 'SkillId', 'skillId') || '');
    var name = String(_skillField(sk, 'Name', 'name') || 'Untitled');
    var desc = String(_skillField(sk, 'Description', 'description') || '');
    var status = String(_skillField(sk, 'Status', 'status') || 'active');
    var tools = String(_skillField(sk, 'Tools', 'tools') || '');
    var tags = String(_skillField(sk, 'Tags', 'tags') || '');
    var enabled = status === 'active';

    var row = document.createElement('article');
    row.className = 'skill-card' + (enabled ? '' : ' skill-card-disabled');
    var head = document.createElement('div');
    head.className = 'skill-card-head';
    var title = document.createElement('div');
    title.className = 'skill-card-title';
    title.textContent = name;
    head.appendChild(title);
    var badge = document.createElement('span');
    badge.className = 'skill-status skill-status-' + status;
    badge.textContent = status.toUpperCase();
    head.appendChild(badge);
    var actions = document.createElement('div');
    actions.className = 'skill-card-actions';
    var editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'auth-save background-inline-button';
    editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', function () { editSkill(sk); });
    actions.appendChild(editBtn);
    var toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'auth-toggle background-inline-button';
    toggleBtn.textContent = enabled ? 'Disable' : 'Enable';
    toggleBtn.addEventListener('click', function () { toggleSkill(id, enabled ? 'disabled' : 'active'); });
    actions.appendChild(toggleBtn);
    var delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'auth-toggle';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', function () { deleteSkill(id); });
    actions.appendChild(delBtn);
    row.appendChild(head);

    if (desc) {
      var d = document.createElement('div');
      d.className = 'skill-card-description';
      d.textContent = desc;
      row.appendChild(d);
    }
    var chips = document.createElement('div');
    chips.className = 'skill-card-chips';
    _skillCsv(tags).forEach(function(tag) {
      var chip = document.createElement('span'); chip.textContent = '#' + tag; chips.appendChild(chip);
    });
    _skillCsv(tools).forEach(function(tool) {
      var chip = document.createElement('span'); chip.className = 'skill-tool-chip'; chip.textContent = tool; chips.appendChild(chip);
    });
    var source = String(_skillField(sk, 'Source', 'source') || 'unknown');
    var sourceChip = document.createElement('span'); sourceChip.className = 'skill-source-chip'; sourceChip.textContent = source; chips.appendChild(sourceChip);
    row.appendChild(chips);
    row.appendChild(actions);
    listEl.appendChild(row);
  });
}

function _buildSkillsWorkspace() {
  var panel = document.getElementById('skillsPanel');
  var container = document.getElementById('idContainer');
  var body = panel && panel.querySelector('.skills-panel-body');
  if (!panel || !container || !body || panel.dataset.workspaceBuilt === 'true') return;
  panel.dataset.workspaceBuilt = 'true';
  panel.classList.add('skills-view');
  container.insertBefore(panel, container.firstChild);

  var intro = body.querySelector('.cog-help');
  var importer = body.querySelector('.skills-import');
  var draft = body.querySelector('.skills-draft');
  var list = document.getElementById('skillsList');
  var oldHeading = Array.prototype.find.call(body.querySelectorAll('.settings-subhead'), function(item) {
    return item.textContent.trim() === 'My skills';
  });

  var toolbar = document.createElement('div');
  toolbar.className = 'skills-view-toolbar';
  toolbar.innerHTML =
    '<input id="skillsSearch" type="search" placeholder="Search names, tags, tools, instructions" aria-label="Search skills">' +
    '<select id="skillsStatusFilter" aria-label="Filter skills by status"><option value="all">All status</option><option value="active">Active</option><option value="draft">Draft</option><option value="disabled">Disabled</option></select>' +
    '<select id="skillsSourceFilter" aria-label="Filter skills by source"><option value="all">All sources</option><option value="paste">Paste</option><option value="url">URL</option><option value="github">GitHub</option><option value="file">File</option><option value="auto-learned">Auto-learned</option><option value="edited">Edited</option></select>' +
    '<select id="skillsSort" aria-label="Sort skills"><option value="updated">Recently updated</option><option value="name">Name</option><option value="status">Status</option></select>' +
    '<span id="skillsViewSummary">0 shown</span>';

  var layout = document.createElement('div');
  layout.className = 'skills-view-layout';
  var library = document.createElement('section');
  library.className = 'skills-library';
  var libraryHeading = document.createElement('div');
  libraryHeading.className = 'skills-column-heading';
  libraryHeading.innerHTML = '<span>SKILL LIBRARY</span><strong>Reusable capabilities</strong>';
  library.append(libraryHeading, list);
  var editor = document.createElement('section');
  editor.className = 'skills-editor';
  var editorHeading = document.createElement('div');
  editorHeading.className = 'skills-column-heading';
  editorHeading.innerHTML = '<span>IMPORT + EDIT</span><strong>Teach Eva a capability</strong>';
  editor.append(editorHeading, intro, importer, draft);
  layout.append(library, editor);
  body.replaceChildren(toolbar, layout);
  if (oldHeading) oldHeading.remove();

  document.getElementById('skillsSearch').addEventListener('input', function(event) {
    _skillsState.query = event.target.value.trim(); renderSkillsList();
  });
  document.getElementById('skillsStatusFilter').addEventListener('change', function(event) {
    _skillsState.statusFilter = event.target.value; renderSkillsList();
  });
  document.getElementById('skillsSourceFilter').addEventListener('change', function(event) {
    _skillsState.sourceFilter = event.target.value; renderSkillsList();
  });
  document.getElementById('skillsSort').addEventListener('change', function(event) {
    _skillsState.sort = event.target.value; renderSkillsList();
  });
}

async function loadSkills() {
  try {
    var data = await _skillsBridge('/v1/skills', { method: 'GET' });
    _skillsState.skills = (data && Array.isArray(data.skills)) ? data.skills : [];
    renderSkillsList();
  } catch (error) {
    var listEl = document.getElementById('skillsList');
    if (listEl) {
      // Build via textContent so an error message (which may contain server
      // text or markup) is never reinterpreted as HTML.
      listEl.innerHTML = '';
      var note = document.createElement('div');
      note.className = 'auth-note';
      note.textContent = (error && error.message) ? String(error.message) : 'Skills unavailable.';
      listEl.appendChild(note);
    }
  }
}

async function toggleSkill(id, status) {
  if (!id) return;
  try {
    await _skillsBridge('/v1/skills/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: status })
    });
    await loadSkills();
  } catch (error) {
    if (typeof setStatus === 'function') setStatus('error', error.message || 'Could not update skill.');
  }
}

async function deleteSkill(id) {
  if (!id) return;
  if (!confirm('Delete this skill?')) return;
  try {
    await _skillsBridge('/v1/skills/' + encodeURIComponent(id), { method: 'DELETE' });
    await loadSkills();
    if (typeof setStatus === 'function') setStatus('info', 'Skill deleted.');
  } catch (error) {
    if (typeof setStatus === 'function') setStatus('error', error.message || 'Could not delete skill.');
  }
}

function initSkills() {
  _buildSkillsWorkspace();
  var typeSel = document.getElementById('skillSourceType');
  if (typeSel) typeSel.addEventListener('change', updateSkillSourceFields);
  var evBtn = document.getElementById('skillEvariseButton');
  if (evBtn) evBtn.addEventListener('click', evariseSkill);
  var clrBtn = document.getElementById('skillImportClearButton');
  if (clrBtn) clrBtn.addEventListener('click', function () { clearSkillImport(); _skillStatus(''); });
  var saveBtn = document.getElementById('skillSaveButton');
  if (saveBtn) saveBtn.addEventListener('click', saveSkill);
  var cancelBtn = document.getElementById('skillDraftCancelButton');
  if (cancelBtn) cancelBtn.addEventListener('click', cancelSkillDraft);
  var closeBtn = document.getElementById('skillsPanelClose');
  if (closeBtn) closeBtn.addEventListener('click', function () { toggleSkillsPanel(false); });
  updateSkillSourceFields();
}

function toggleSkillsPanel(force) {
  var panel = document.getElementById('skillsPanel');
  if (!panel) return;
  var open = document.body.classList.contains('skills-view-open');
  var shouldOpen = typeof force === 'boolean' ? force : !open;
  if (!shouldOpen) {
    document.body.classList.remove('skills-view-open');
    panel.setAttribute('aria-hidden', 'true');
    return;
  }
  if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
  if (window.EvaAssets && typeof window.EvaAssets.close === 'function') window.EvaAssets.close();
  if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench();
  if (typeof closeSidePanels === 'function') closeSidePanels();
  document.body.classList.add('skills-view-open');
  panel.setAttribute('aria-hidden', 'false');
  loadSkills();
}

window.EvaSkills = {
  open: function() { toggleSkillsPanel(true); },
  close: function() { toggleSkillsPanel(false); },
  refresh: loadSkills
};
