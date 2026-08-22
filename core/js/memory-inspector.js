// Structured memory inspection and maintainer controls.
(function () {
  'use strict';

  var state = { data: null, selected: null, detail: null, pendingDeleteId: '' };

  function bridge(path, options) {
    if (typeof backgroundBridgeRequest !== 'function') return Promise.reject(new Error('Bridge unavailable'));
    return backgroundBridgeRequest(path, options);
  }

  function status(message, isError) {
    var element = document.getElementById('memoryInspectorStatus');
    if (!element) return;
    element.textContent = message || '';
    element.style.color = isError ? '#d66' : '';
  }

  function recordValue(record, field, fallback) {
    return String(record && (record[field] !== undefined ? record[field] : fallback) || '');
  }

  function clear(element) {
    while (element && element.firstChild) element.removeChild(element.firstChild);
  }

  function inputValue(id) {
    var element = document.getElementById(id);
    return element ? element.value.trim() : '';
  }

  function setInputValue(id, value) {
    var element = document.getElementById(id);
    if (element) element.value = String(value || '');
  }

  function formatValue(value) {
    return value === undefined || value === null || value === '' ? 'Not set' : String(value);
  }

  function pill(text, className) {
    var element = document.createElement('span');
    element.className = 'memory-inspector-pill ' + (className || '');
    element.textContent = text;
    return element;
  }

  function atomType(atom) {
    var key = recordValue(atom, 'Kind', 'fact').toLowerCase();
    var types = {
      preference: { label: 'Preferences', description: 'Confirmed choices and working style' },
      fact: { label: 'Facts', description: 'Attributed information and recalled context' },
      constraint: { label: 'Constraints', description: 'Boundaries and requirements' },
      decision: { label: 'Decisions', description: 'Recorded choices and outcomes' },
      identity_claim: { label: 'Identity claims', description: 'Reviewable identity statements' },
      candidate: { label: 'Candidates', description: 'Unconfirmed memory candidates' }
    };
    return types[key] || { key: key || 'other', label: 'Other records', description: 'Records without a recognized type' };
  }

  function atomGroups(atoms) {
    var orderedKeys = ['preference', 'fact', 'constraint', 'decision', 'identity_claim', 'candidate'];
    var groups = {};
    atoms.forEach(function (atom) {
      var type = atomType(atom);
      var key = recordValue(atom, 'Kind', 'fact').toLowerCase();
      if (!groups[key]) groups[key] = { key: key, label: type.label, description: type.description, atoms: [] };
      groups[key].atoms.push(atom);
    });
    return Object.keys(groups).sort(function (left, right) {
      var leftIndex = orderedKeys.indexOf(left);
      var rightIndex = orderedKeys.indexOf(right);
      if (leftIndex < 0) leftIndex = orderedKeys.length;
      if (rightIndex < 0) rightIndex = orderedKeys.length;
      return leftIndex - rightIndex || groups[left].label.localeCompare(groups[right].label);
    }).map(function (key) { return groups[key]; });
  }

  function atomGroup(group, search, kindFilter) {
    var details = document.createElement('details');
    details.className = 'memory-atom-group';
    var activeCount = group.atoms.filter(function (atom) { return recordValue(atom, 'Status') === 'active'; }).length;
    details.open = !!search || kindFilter === group.key || (activeCount > 0 && group.atoms.length <= 8);
    var summary = document.createElement('summary');
    var title = document.createElement('span');
    title.className = 'memory-atom-group-title';
    var heading = document.createElement('strong');
    heading.textContent = group.label;
    var description = document.createElement('small');
    description.textContent = group.description;
    title.append(heading, description);
    var count = document.createElement('span');
    count.className = 'memory-atom-group-count';
    count.textContent = activeCount + ' active / ' + group.atoms.length;
    summary.append(title, count);
    var list = document.createElement('div');
    list.className = 'memory-atom-group-list';
    group.atoms.forEach(function (atom) { list.appendChild(atomCard(atom)); });
    details.append(summary, list);
    return details;
  }

  function closeAtomDetail() {
    var dialog = document.getElementById('memoryAtomDetailDialog');
    if (!dialog) return;
    dialog.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('memory-atom-detail-open');
    state.detail = null;
    state.pendingDeleteId = '';
  }

  function detailLine(container, label, value) {
    var term = document.createElement('dt');
    term.textContent = label;
    var definition = document.createElement('dd');
    definition.textContent = formatValue(value);
    container.append(term, definition);
  }

  function detailAssociation(container, title, entries) {
    var section = document.createElement('div');
    var heading = document.createElement('h4');
    heading.textContent = title;
    var value = document.createElement('p');
    value.textContent = entries && entries.length ? entries.join('\n') : 'None';
    section.append(heading, value);
    container.appendChild(section);
  }

  function atomPurpose(atom, detail) {
    var status = recordValue(atom, 'Status');
    var kind = recordValue(atom, 'Kind', 'fact');
    var scope = recordValue(atom, 'Scope');
    var confidence = Number(recordValue(atom, 'Confidence', '0'));
    var expiresAt = recordValue(atom, 'ExpiresAt');
    var expiresAtMs = expiresAt ? Date.parse(expiresAt) : NaN;
    if (status !== 'active') return 'This record is kept for audit history only. Eva does not use it in new replies because it is ' + status + '.';
    if (Number.isFinite(expiresAtMs) && expiresAtMs <= Date.now()) return 'This record has expired. It remains visible for review, but Eva does not use it in new replies.';
    var uses = [];
    if (recordValue(atom, 'Entity').toLowerCase() === 'user' && scope === 'user' && confidence >= 0.5) {
      uses.push('Eva can recall this as user-profile context in future replies');
    } else if (kind === 'preference') {
      uses.push('This is a preference record; it becomes response-style guidance after a derived preference is approved');
    } else {
      uses.push('This is a traceable ' + kind.replace(/_/g, ' ') + ' record; it is not automatically added to the user profile');
    }
    var scenarioCount = detail && Array.isArray(detail.scenarios) ? detail.scenarios.length : 0;
    if (scenarioCount) uses.push('it can also supply context when one of its ' + scenarioCount + ' associated scenarios is active');
    var approvedTraits = detail && Array.isArray(detail.traits) ? detail.traits.filter(function (trait) {
      return recordValue(trait, 'Status') === 'approved' && recordValue(trait, 'Scope') === 'user';
    }) : [];
    if (approvedTraits.length) uses.push('an approved derived preference currently uses this evidence');
    return uses.join('. ') + '. Recalled memory is supplied to Eva as data, not as instructions.';
  }

  function renderAtomDetail() {
    var detail = state.detail;
    var selected = state.selected || {};
    var atom = detail && detail.atom ? detail.atom : selected;
    var title = document.getElementById('memoryAtomDetailTitle');
    var metadata = document.getElementById('memoryAtomDetailMetadata');
    var purpose = document.getElementById('memoryAtomDetailPurpose');
    var associations = document.getElementById('memoryAtomDetailAssociations');
    var form = document.getElementById('memoryAtomDetailForm');
    var stateLabel = document.getElementById('memoryAtomDetailState');
    if (!atom || !title || !metadata || !associations || !form) return;
    title.textContent = recordValue(atom, 'Entity', 'memory') + ' / ' + recordValue(atom, 'Relation', 'record');
    clear(metadata);
    [
      ['Memory ID', recordValue(atom, 'MemoryId')],
      ['Status', recordValue(atom, 'Status')],
      ['Created', recordValue(atom, 'CreatedAt')],
      ['Last updated', recordValue(atom, 'UpdatedAt')],
      ['Original source', recordValue(atom, 'SourceRef')],
      ['Supersedes', recordValue(atom, 'SupersedesId')]
    ].forEach(function (item) { detailLine(metadata, item[0], item[1]); });
    if (purpose) purpose.textContent = atomPurpose(atom, detail);
    clear(associations);
    if (detail) {
      detailAssociation(associations, 'Evidence', (detail.evidence || []).map(function (item) {
        return formatValue(item.SourceType) + ': ' + formatValue(item.SourceRef) + ' (' + formatValue(item.CreatedAt) + ')';
      }));
      detailAssociation(associations, 'Scenario memberships', (detail.scenarios || []).map(function (item) {
        return formatValue(item.Title) + ' [' + formatValue(item.Role) + '] ' + formatValue(item.Scope) + ':' + formatValue(item.ScopeId);
      }));
      detailAssociation(associations, 'Derived preferences', (detail.traits || []).map(function (item) {
        return formatValue(item.Trait) + ': ' + formatValue(item.Value) + ' [' + formatValue(item.Status) + ']';
      }));
      detailAssociation(associations, 'Corrected from', detail.superseded_atom ? [
        formatValue(detail.superseded_atom.Entity) + ' / ' + formatValue(detail.superseded_atom.Relation) + ': ' + formatValue(detail.superseded_atom.Value)
      ] : []);
      detailAssociation(associations, 'Later corrections', (detail.corrections || []).map(function (item) {
        return formatValue(item.Entity) + ' / ' + formatValue(item.Relation) + ': ' + formatValue(item.Value) + ' [' + formatValue(item.Status) + ']';
      }));
    } else {
      detailAssociation(associations, 'Associations', ['Loading provenance and usage details...']);
    }
    setInputValue('memoryAtomEntity', recordValue(atom, 'Entity'));
    setInputValue('memoryAtomRelation', recordValue(atom, 'Relation'));
    setInputValue('memoryAtomKind', recordValue(atom, 'Kind', 'fact'));
    setInputValue('memoryAtomTrust', recordValue(atom, 'Trust') === 'unconfirmed' ? 'user_confirmed' : recordValue(atom, 'Trust', 'user_confirmed'));
    setInputValue('memoryAtomScope', recordValue(atom, 'Scope', 'user'));
    setInputValue('memoryAtomScopeId', recordValue(atom, 'ScopeId'));
    setInputValue('memoryAtomConfidence', recordValue(atom, 'Confidence', '0.5'));
    setInputValue('memoryAtomExpiresAt', recordValue(atom, 'ExpiresAt'));
    setInputValue('memoryAtomSourceRef', 'maintainer-correction:' + recordValue(atom, 'MemoryId'));
    setInputValue('memoryAtomValue', recordValue(atom, 'Value'));
    var active = recordValue(atom, 'Status') === 'active';
    var deleted = recordValue(atom, 'Status') === 'deleted';
    Array.prototype.forEach.call(form.elements, function (element) {
      element.disabled = !active && element.id !== 'memoryAtomDetailRemove';
    });
    if (stateLabel) stateLabel.textContent = active ? '' : 'This record is ' + formatValue(atom.Status) + '. Correction fields are locked, but it can still be removed from recall history.';
    var removeButton = document.getElementById('memoryAtomDetailRemove');
    if (removeButton) removeButton.disabled = deleted;
  }

  async function openAtom(atom) {
    var dialog = document.getElementById('memoryAtomDetailDialog');
    if (!dialog) return;
    state.selected = atom;
    state.detail = null;
    state.pendingDeleteId = '';
    dialog.setAttribute('aria-hidden', 'false');
    document.body.classList.add('memory-atom-detail-open');
    renderAtomDetail();
    try {
      var detail = await bridge('/v1/memory/atoms/' + encodeURIComponent(recordValue(atom, 'MemoryId')), { method: 'GET' });
      if (state.selected === atom) {
        state.detail = detail;
        renderAtomDetail();
      }
    } catch (error) {
      var stateLabel = document.getElementById('memoryAtomDetailState');
      if (stateLabel) stateLabel.textContent = error.message || 'Could not load record provenance.';
    }
  }

  async function correctAtom(event) {
    if (event) event.preventDefault();
    var atom = state.detail && state.detail.atom;
    if (!atom || recordValue(atom, 'Status') !== 'active') return;
    var confidence = Number(inputValue('memoryAtomConfidence'));
    var value = inputValue('memoryAtomValue');
    if (!value || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      var stateLabel = document.getElementById('memoryAtomDetailState');
      if (stateLabel) stateLabel.textContent = 'Provide a memory value and a confidence between 0 and 1.';
      return;
    }
    try {
      await bridge('/v1/memory/atoms/' + encodeURIComponent(recordValue(atom, 'MemoryId')), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replacement: {
          entity: inputValue('memoryAtomEntity'), relation: inputValue('memoryAtomRelation'), kind: inputValue('memoryAtomKind'),
          trust: inputValue('memoryAtomTrust'), scope: inputValue('memoryAtomScope'), scope_id: inputValue('memoryAtomScopeId'),
          confidence: confidence, expires_at: inputValue('memoryAtomExpiresAt'), source_ref: inputValue('memoryAtomSourceRef'), value: value
        } })
      });
      closeAtomDetail();
      status('Memory corrected; the original remains in its audit trail.');
      load();
    } catch (error) {
      var stateLabel = document.getElementById('memoryAtomDetailState');
      if (stateLabel) stateLabel.textContent = error.message || 'Could not correct memory.';
    }
  }

  async function deleteAtom() {
    var atom = state.detail && state.detail.atom;
    if (!atom || recordValue(atom, 'Status') === 'deleted') return;
    var memoryId = recordValue(atom, 'MemoryId');
    var button = document.getElementById('memoryAtomDetailRemove');
    if (state.pendingDeleteId !== memoryId) {
      state.pendingDeleteId = memoryId;
      if (button) button.textContent = 'Confirm removal';
      var stateLabel = document.getElementById('memoryAtomDetailState');
      if (stateLabel) stateLabel.textContent = 'Select Confirm removal to remove this record from future recall.';
      return;
    }
    try {
      await bridge('/v1/memory/atoms/' + encodeURIComponent(memoryId), { method: 'DELETE' });
      closeAtomDetail();
      status('Memory removed from future recall.');
      load();
    } catch (error) {
      var label = document.getElementById('memoryAtomDetailState');
      if (label) label.textContent = error.message || 'Could not remove memory.';
    }
  }

  async function startFreshMemory() {
    if (typeof evaTextPrompt !== 'function') {
      status('The confirmation dialog is unavailable.', true);
      return;
    }
    var confirmation = await evaTextPrompt('Start fresh memory', '', {
      maxLength: 20, placeholder: 'Type START FRESH', kind: 'memory_fresh_start'
    });
    if (confirmation !== 'START FRESH') {
      status('Fresh start cancelled. Type START FRESH to confirm.');
      return;
    }
    try {
      var result = await bridge('/v1/memory/start-fresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmation: 'START_FRESH_MEMORY' })
      });
      closeAtomDetail();
      state.data = null;
      status('Fresh memory started. Previous memory was backed up locally.');
      load();
      return result;
    } catch (error) {
      status(error.message || 'Could not start fresh memory.', true);
      return null;
    }
  }

  async function promoteTrait(atom) {
    var trait = window.prompt('Preference name:', recordValue(atom, 'Relation', 'preference'));
    if (trait === null || !trait.trim()) return;
    var value = window.prompt('Preference value:', recordValue(atom, 'Value'));
    if (value === null || !value.trim()) return;
    try {
      await bridge('/v1/memory/traits', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trait: trait, value: value, source_memory_ids: [recordValue(atom, 'MemoryId')] })
      });
      status('Persona preference approved.');
      load();
    } catch (error) {
      status(error.message || 'Could not approve preference.', true);
    }
  }

  function preferenceCandidate(atom) {
    return recordValue(atom, 'Status') === 'active' &&
      ['user_confirmed', 'operator_approved'].indexOf(recordValue(atom, 'Trust')) >= 0 &&
      (recordValue(atom, 'Kind') === 'preference' || /preference|style|format|detail/.test(recordValue(atom, 'Relation').toLowerCase()));
  }

  async function reviewProposal(proposal, decision) {
    try {
      await bridge('/v1/memory/growth-proposals/' + encodeURIComponent(recordValue(proposal, 'ProposalId')) + '/' + decision, { method: 'POST' });
      status('Proposal ' + (decision === 'approve' ? 'approved.' : 'rejected.'));
      load();
    } catch (error) {
      status(error.message || 'Could not review proposal.', true);
    }
  }

  function atomCard(atom) {
    var card = document.createElement('article');
    card.className = 'memory-inspector-card';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', 'Inspect memory record: ' + recordValue(atom, 'Entity', 'memory') + ' ' + recordValue(atom, 'Relation', 'fact'));
    card.addEventListener('click', function (event) {
      if (!event.target.closest('button')) openAtom(atom);
    });
    card.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openAtom(atom);
      }
    });
    var heading = document.createElement('strong');
    heading.textContent = recordValue(atom, 'Entity', 'memory') + ' / ' + recordValue(atom, 'Relation', 'fact');
    var value = document.createElement('div');
    value.className = 'memory-inspector-value';
    value.textContent = recordValue(atom, 'Value');
    var meta = document.createElement('div');
    meta.className = 'memory-inspector-meta';
    meta.append(pill(recordValue(atom, 'Kind', 'fact'), 'kind-' + recordValue(atom, 'Kind', 'fact')));
    meta.append(pill(recordValue(atom, 'Trust'), 'trust-' + recordValue(atom, 'Trust')));
    meta.append(pill(recordValue(atom, 'Scope')));
    meta.append(pill(recordValue(atom, 'Status')));
    var actions = document.createElement('div');
    actions.className = 'memory-inspector-actions';
    if (recordValue(atom, 'Status') === 'active') {
      if (preferenceCandidate(atom)) {
        var preference = document.createElement('button');
        preference.type = 'button'; preference.className = 'auth-save background-inline-button'; preference.textContent = 'Use preference';
        preference.addEventListener('click', function () { promoteTrait(atom); });
        actions.appendChild(preference);
      }
      var inspect = document.createElement('button');
      inspect.type = 'button'; inspect.className = 'auth-save background-inline-button'; inspect.textContent = 'Inspect';
      inspect.addEventListener('click', function () { openAtom(atom); });
      actions.appendChild(inspect);
    }
    card.append(heading, value, meta, actions);
    return card;
  }

  function traitCard(trait) {
    var card = document.createElement('article');
    card.className = 'memory-inspector-card';
    var heading = document.createElement('strong');
    heading.textContent = recordValue(trait, 'Trait');
    var value = document.createElement('div');
    value.className = 'memory-inspector-value';
    value.textContent = recordValue(trait, 'Value');
    var meta = document.createElement('div');
    meta.className = 'memory-inspector-meta';
    meta.append(pill(recordValue(trait, 'Status')));
    meta.append(pill(recordValue(trait, 'Scope')));
    card.append(heading, value, meta);
    return card;
  }

  function proposalCard(proposal) {
    var card = document.createElement('article');
    card.className = 'memory-inspector-card';
    var heading = document.createElement('strong');
    heading.textContent = recordValue(proposal, 'Kind', 'growth') + ' proposal';
    var meta = document.createElement('div');
    meta.className = 'memory-inspector-meta';
    meta.append(pill(recordValue(proposal, 'RiskLevel')));
    meta.append(pill(recordValue(proposal, 'Status')));
    card.append(heading, meta);
    if (recordValue(proposal, 'Status') === 'proposed') {
      var actions = document.createElement('div');
      actions.className = 'memory-inspector-actions';
      ['approve', 'reject'].forEach(function (decision) {
        var button = document.createElement('button');
        button.type = 'button'; button.className = decision === 'approve' ? 'auth-save background-inline-button' : 'auth-toggle background-inline-button';
        button.textContent = decision === 'approve' ? 'Approve' : 'Reject';
        button.addEventListener('click', function () { reviewProposal(proposal, decision); });
        actions.appendChild(button);
      });
      card.appendChild(actions);
    }
    return card;
  }

  function render() {
    var data = state.data || {};
    var scenarioTitle = document.getElementById('memoryScenarioTitle');
    if (scenarioTitle) scenarioTitle.textContent = data.scenario ? (recordValue(data.scenario, 'Title') || 'Continuing conversation') : 'No active scenario';
    var traits = document.getElementById('memoryTraitsList');
    var atoms = document.getElementById('memoryAtomsList');
    var growth = document.getElementById('memoryGrowthList');
    [traits, atoms, growth].forEach(clear);
    (data.traits || []).forEach(function (item) { traits.appendChild(traitCard(item)); });
    var allAtoms = data.atoms || [];
    var search = inputValue('memoryAtomSearch').toLowerCase();
    var statusFilter = inputValue('memoryAtomStatusFilter');
    var kindFilter = inputValue('memoryAtomKindFilter');
    var scopeFilter = inputValue('memoryAtomScopeFilter');
    var sort = inputValue('memoryAtomSort') || 'updated';
    var filteredAtoms = allAtoms.filter(function (item) {
      var text = [item.Entity, item.Relation, item.Value, item.Kind, item.Trust, item.Scope, item.SourceRef, item.MemoryId].join(' ').toLowerCase();
      return (!search || text.indexOf(search) >= 0) && (!statusFilter || recordValue(item, 'Status') === statusFilter) &&
        (!kindFilter || recordValue(item, 'Kind') === kindFilter) && (!scopeFilter || recordValue(item, 'Scope') === scopeFilter);
    });
    filteredAtoms.sort(function (left, right) {
      if (sort === 'confidence') return Number(right.Confidence || 0) - Number(left.Confidence || 0);
      var field = sort === 'created' ? 'CreatedAt' : 'UpdatedAt';
      return String(right[field] || '').localeCompare(String(left[field] || ''));
    });
    atomGroups(filteredAtoms).forEach(function (group) { atoms.appendChild(atomGroup(group, search, kindFilter)); });
    if (!filteredAtoms.length && atoms) {
      var empty = document.createElement('p');
      empty.className = 'memory-atoms-empty';
      empty.textContent = 'No memory records match these filters.';
      atoms.appendChild(empty);
    }
    var count = document.getElementById('memoryAtomCount');
    if (count) count.textContent = filteredAtoms.length + ' of ' + allAtoms.length + ' records';
    (data.growth_proposals || []).forEach(function (item) { growth.appendChild(proposalCard(item)); });
    if (!traits || !traits.children.length) status('No approved traits or active proposals are recorded.');
  }

  async function load() {
    var sessionId = typeof ensureActiveSessionId === 'function' ? ensureActiveSessionId() : '';
    try {
      state.data = await bridge('/v1/memory/inspector?session_id=' + encodeURIComponent(sessionId), { method: 'GET' });
      render();
    } catch (error) {
      status(error.message || 'Memory inspector unavailable.', true);
    }
  }

  function open(force) {
    var panel = document.getElementById('memoryInspectorPanel');
    if (!panel) return;
    var shouldOpen = typeof force === 'boolean' ? force : !document.body.classList.contains('memory-view-open');
    if (!shouldOpen) {
      document.body.classList.remove('memory-view-open');
      panel.setAttribute('aria-hidden', 'true');
      return;
    }
    if (typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
    if (typeof closeVoiceView === 'function') closeVoiceView();
    if (window.EvaAssets && typeof window.EvaAssets.close === 'function') window.EvaAssets.close();
    if (window.EvaSkills && typeof window.EvaSkills.close === 'function') window.EvaSkills.close();
    if (window.EvaWorkspaces && typeof window.EvaWorkspaces.closeWorkbench === 'function') window.EvaWorkspaces.closeWorkbench();
    if (typeof closeSidePanels === 'function') closeSidePanels('memoryInspectorPanel');
    document.body.classList.add('memory-view-open');
    panel.setAttribute('aria-hidden', 'false');
    load();
  }

  function init() {
    var close = document.getElementById('memoryInspectorClose');
    var refresh = document.getElementById('memoryInspectorRefresh');
    var startFresh = document.getElementById('memoryInspectorStartFresh');
    var detailClose = document.getElementById('memoryAtomDetailClose');
    var detailForm = document.getElementById('memoryAtomDetailForm');
    var detailRemove = document.getElementById('memoryAtomDetailRemove');
    if (close) close.addEventListener('click', function () { open(false); });
    if (refresh) refresh.addEventListener('click', load);
    if (startFresh) startFresh.addEventListener('click', startFreshMemory);
    ['memoryAtomSearch', 'memoryAtomStatusFilter', 'memoryAtomKindFilter', 'memoryAtomScopeFilter', 'memoryAtomSort'].forEach(function (id) {
      var control = document.getElementById(id);
      if (control) control.addEventListener(id === 'memoryAtomSearch' ? 'input' : 'change', render);
    });
    if (detailClose) detailClose.addEventListener('click', closeAtomDetail);
    if (detailForm) detailForm.addEventListener('submit', correctAtom);
    if (detailRemove) detailRemove.addEventListener('click', deleteAtom);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && state.detail) closeAtomDetail();
    });
  }

  window.EvaMemoryInspector = {
    open: function () { open(true); },
    close: function () { open(false); },
    toggle: function (event) {
      if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
      open();
    },
    refresh: load
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
}());