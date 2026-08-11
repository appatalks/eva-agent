// Structured memory inspection and maintainer controls.
(function () {
  'use strict';

  var state = { data: null, selected: null };

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

  function pill(text, className) {
    var element = document.createElement('span');
    element.className = 'memory-inspector-pill ' + (className || '');
    element.textContent = text;
    return element;
  }

  async function correctAtom(atom) {
    var value = window.prompt('Correct this memory record:', recordValue(atom, 'Value'));
    if (value === null || !value.trim()) return;
    try {
      await bridge('/v1/memory/atoms/' + encodeURIComponent(recordValue(atom, 'MemoryId')), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replacement: { value: value, source_ref: 'maintainer-correction', trust: 'user_confirmed' } })
      });
      status('Memory corrected.');
      load();
    } catch (error) {
      status(error.message || 'Could not correct memory.', true);
    }
  }

  async function deleteAtom(atom) {
    if (!window.confirm('Remove this memory from future recall?')) return;
    try {
      await bridge('/v1/memory/atoms/' + encodeURIComponent(recordValue(atom, 'MemoryId')), { method: 'DELETE' });
      status('Memory removed.');
      load();
    } catch (error) {
      status(error.message || 'Could not remove memory.', true);
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
    var heading = document.createElement('strong');
    heading.textContent = recordValue(atom, 'Entity', 'memory') + ' / ' + recordValue(atom, 'Relation', 'fact');
    var value = document.createElement('div');
    value.className = 'memory-inspector-value';
    value.textContent = recordValue(atom, 'Value');
    var meta = document.createElement('div');
    meta.className = 'memory-inspector-meta';
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
      var correct = document.createElement('button');
      correct.type = 'button'; correct.className = 'auth-save background-inline-button'; correct.textContent = 'Correct';
      correct.addEventListener('click', function () { correctAtom(atom); });
      actions.appendChild(correct);
      var remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'auth-toggle background-inline-button'; remove.textContent = 'Remove';
      remove.addEventListener('click', function () { deleteAtom(atom); });
      actions.appendChild(remove);
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
    (data.atoms || []).forEach(function (item) { atoms.appendChild(atomCard(item)); });
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
    if (close) close.addEventListener('click', function () { open(false); });
    if (refresh) refresh.addEventListener('click', load);
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