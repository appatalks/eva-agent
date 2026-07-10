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

var _skillsState = { skills: [], draft: null };

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
  var wrap = document.getElementById('skillDraft');
  if (wrap) wrap.style.display = 'none';
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
    await _skillsBridge('/v1/skills', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(skill)
    });
    cancelSkillDraft();
    clearSkillImport();
    await loadSkills();
    if (typeof setStatus === 'function') setStatus('info', 'Skill saved.');
    _skillStatus('Skill saved.');
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

function renderSkillsList() {
  var listEl = document.getElementById('skillsList');
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!_skillsState.skills.length) {
    var empty = document.createElement('div');
    empty.className = 'auth-note';
    empty.textContent = 'No skills yet. Import one above.';
    listEl.appendChild(empty);
    return;
  }
  _skillsState.skills.forEach(function (sk) {
    var id = String(_skillField(sk, 'SkillId', 'skillId') || '');
    var name = String(_skillField(sk, 'Name', 'name') || 'Untitled');
    var desc = String(_skillField(sk, 'Description', 'description') || '');
    var status = String(_skillField(sk, 'Status', 'status') || 'active');
    var tools = String(_skillField(sk, 'Tools', 'tools') || '');
    var tags = String(_skillField(sk, 'Tags', 'tags') || '');
    var enabled = status === 'active';

    var row = document.createElement('div');
    row.className = 'background-row';
    var head = document.createElement('div');
    head.className = 'background-row-head';
    var title = document.createElement('div');
    title.className = 'background-title';
    title.textContent = name + (enabled ? '' : ' (disabled)');
    head.appendChild(title);
    var actions = document.createElement('div');
    actions.className = 'background-actions';
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
    head.appendChild(actions);
    row.appendChild(head);

    if (desc) {
      var d = document.createElement('div');
      d.className = 'background-description';
      d.textContent = desc;
      row.appendChild(d);
    }
    var metaBits = [];
    if (tools) metaBits.push('Tools: ' + tools);
    if (tags) metaBits.push('Tags: ' + tags);
    if (metaBits.length) {
      var meta = document.createElement('div');
      meta.className = 'background-meta';
      metaBits.forEach(function (t) { var s = document.createElement('span'); s.textContent = t; meta.appendChild(s); });
      row.appendChild(meta);
    }
    listEl.appendChild(row);
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
    var mutationEnvelope = (typeof captureMutationEnvelope === 'function')
      ? captureMutationEnvelope() : null;
    await _skillsBridge('/v1/skills/' + encodeURIComponent(id), {
      method: 'DELETE', evaMutationEnvelope: mutationEnvelope
    });
    await loadSkills();
    if (typeof setStatus === 'function') setStatus('info', 'Skill deleted.');
  } catch (error) {
    if (typeof setStatus === 'function') setStatus('error', error.message || 'Could not delete skill.');
  }
}

function initSkills() {
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

// Slide-in Skills panel toggled from the sidebar. Loads the list on open so it
// always reflects the latest saved skills. Pass false to force-close.
function toggleSkillsPanel(force) {
  var panel = document.getElementById('skillsPanel');
  if (!panel) return;
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  var next = (typeof force === 'boolean') ? !force : visible;
  panel.setAttribute('aria-hidden', next ? 'true' : 'false');
  if (!next) loadSkills();
}
