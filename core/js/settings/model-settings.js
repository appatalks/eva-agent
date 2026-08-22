// Model settings, selector presentation, and parameter-policy helpers.
var DEFAULT_REASONING_EFFORT = 'high';

function getModelTemperature() {
  var el = document.getElementById('sldTemperature');
  return el ? parseFloat(el.value) : 0.7;
}

function normalizeModelMaxTokens(rawValue) {
  var text = String(rawValue == null ? '' : rawValue).trim();
  if (!/^[0-9]+$/.test(text)) return null;
  var value = Number(text);
  if (!Number.isSafeInteger(value)) return null;
  return Math.max(1, Math.min(128000, value));
}

function getModelMaxTokens() {
  var el = document.getElementById('txtMaxTokens');
  var value = normalizeModelMaxTokens(el ? el.value : '16384');
  if (value === null) {
    value = 16384;
    if (typeof setStatus === 'function') setStatus('warn', 'Max Tokens must be a whole number; reset to 16,384.');
  }
  if (el) el.value = String(value);
  return value;
}

function reportCompletionTruncation(data) {
  var finishReason = data && data.choices && data.choices[0] && data.choices[0].finish_reason;
  if (finishReason !== 'length') return false;
  if (typeof setStatus === 'function') {
    setStatus('warn', 'Eva reached Max Tokens; this response may be incomplete.');
  }
  return true;
}

function getReasoningEffort() {
  var el = document.getElementById('selReasoningEffort');
  var value = el ? el.value : DEFAULT_REASONING_EFFORT;
  return ['default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].indexOf(value) >= 0 ? value : DEFAULT_REASONING_EFFORT;
}

function getReasoningEffortForModel(model) {
  var effort = getReasoningEffort();
  if (model === 'copilot-acp' || model === 'aig') return effort;
  return ['low', 'medium', 'high'].indexOf(effort) >= 0 ? effort : 'default';
}

var DIRECT_OPENAI_MODEL_INFO = {
  'openai:gpt-5.6-luna': { role: 'Fast, cost-sensitive conversation', input: '$0.20', output: '$1.20', efforts: ['default', 'none', 'low', 'medium', 'high', 'xhigh', 'max'] },
  'openai:gpt-5.6-terra': { role: 'Balanced intelligence and cost', input: '$2.00', output: '$12.00', efforts: ['default', 'none', 'low', 'medium', 'high', 'xhigh', 'max'] },
  'openai:gpt-5.6-sol': { role: 'Premium complex reasoning', input: '$5.00', output: '$30.00', efforts: ['default', 'none', 'low', 'medium', 'high', 'xhigh', 'max'] },
  'openai:gpt-4.1-nano': { role: 'Lightweight routing and classification', input: '$0.10', output: '$0.40', efforts: ['default'] },
  'openai:gpt-5.2': { role: 'Previous-frontier professional work', input: '$1.75', output: '$14.00', efforts: ['default', 'none', 'low', 'medium', 'high', 'xhigh'] },
  'openai:gpt-5': { role: 'General reasoning', input: '$1.25', output: '$10.00', efforts: ['default', 'minimal', 'low', 'medium', 'high'] },
  'openai:gpt-5-mini': { role: 'Efficient routine reasoning', input: '$0.25', output: '$2.00', efforts: ['default', 'minimal', 'low', 'medium', 'high'] },
  'openai:gpt-4.1': { role: 'Reliable non-reasoning work', input: '$2.00', output: '$8.00', efforts: ['default'] },
  'openai:gpt-4o': { role: 'General conversation and vision', input: '$2.50', output: '$10.00', efforts: ['default'] },
  'openai:o3': { role: 'Deep analysis and hard problems', input: '$2.00', output: '$8.00', efforts: ['default', 'low', 'medium', 'high'] },
  'openai:o3-mini': { role: 'Compact analytical reasoning', input: '$1.10', output: '$4.40', efforts: ['default', 'low', 'medium', 'high'] }
};

function getDirectOpenAIReasoningEfforts(model) {
  if (DIRECT_OPENAI_MODEL_INFO[model]) return DIRECT_OPENAI_MODEL_INFO[model].efforts;
  if (model.indexOf('openai:') === 0) return ['default'];
  return null;
}

function updateAIGModelInfo() {
  var model = (document.getElementById('selAIGBackend') || {}).value || '';
  var info = DIRECT_OPENAI_MODEL_INFO[model];
  var panel = document.getElementById('aigModelInfo');
  if (!panel) return;
  panel.style.display = info ? 'grid' : 'none';
  if (!info) return;
  var role = document.getElementById('aigModelRole');
  var input = document.getElementById('aigModelInputCost');
  var output = document.getElementById('aigModelOutputCost');
  if (role) role.textContent = info.role;
  if (input) input.textContent = info.input;
  if (output) output.textContent = info.output;
}

var _modeInitDone = false;

function setModelSettingsModeInitialized() {
  _modeInitDone = true;
}

function onModelSettingsChange() {
  var sel = document.getElementById('selModel');
  if (!sel) return;
  var model = sel.value;
  var reOpt = document.getElementById('opt-reasoningEffort');
  if (reOpt) {
      var reasoningModels = ['o3-mini', 'copilot-acp'];
    var aigBackend = (document.getElementById('selAIGBackend') || {}).value || '';
    var cognitionEnabled = !!(document.getElementById('cogEnabled') || {}).checked;
    var cognitionModels = ['cogEvaModel', 'cogReviewerModel'].map(function (id) {
      return (document.getElementById(id) || {}).value || '';
    });
    var cognitionUsesCloud = cognitionEnabled && cognitionModels.some(function (cognitionModel) {
      return cognitionModel && cognitionModel !== 'lmstudio';
    });
    var directOpenAIEfforts = model === 'aig' ? getDirectOpenAIReasoningEfforts(aigBackend) : null;
    var supportsReasoning = reasoningModels.indexOf(model) >= 0 || (model === 'aig' && (
      directOpenAIEfforts ? directOpenAIEfforts.length > 1 : (aigBackend !== 'lmstudio' || cognitionUsesCloud)
    ));
    reOpt.style.display = supportsReasoning ? 'block' : 'none';
    var reasoningSelect = document.getElementById('selReasoningEffort');
    if (reasoningSelect) {
      var savedEffort = localStorage.getItem('reasoningEffort') || DEFAULT_REASONING_EFFORT;
      var hasSavedEffort = Array.from(reasoningSelect.options).some(function (option) { return option.value === savedEffort; });
      if (!hasSavedEffort) savedEffort = DEFAULT_REASONING_EFFORT;
      var supportsFullReasoningRange = model === 'copilot-acp' || (model === 'aig' && !directOpenAIEfforts);
      var allowedEfforts = directOpenAIEfforts || (supportsFullReasoningRange ? null : ['default', 'low', 'medium', 'high']);
      Array.from(reasoningSelect.options).forEach(function (option) {
        option.disabled = !!allowedEfforts && allowedEfforts.indexOf(option.value) < 0;
      });
      if (allowedEfforts && allowedEfforts.indexOf(savedEffort) < 0) {
        savedEffort = allowedEfforts.indexOf(DEFAULT_REASONING_EFFORT) >= 0 ? DEFAULT_REASONING_EFFORT : 'default';
        localStorage.setItem('reasoningEffort', savedEffort);
      }
      reasoningSelect.value = savedEffort;
    }
  }
  var tempOpt = document.getElementById('opt-temperature');
  if (tempOpt) {
      var hideTemp = ['o3-mini', 'gpt-5-mini', 'latest', 'copilot-acp', 'aig'].indexOf(model) >= 0;
    tempOpt.style.display = hideTemp ? 'none' : 'block';
  }
  var aigOpt = document.getElementById('opt-aigBackend');
  if (aigOpt) aigOpt.style.display = model === 'aig' ? 'block' : 'none';
  updateAIGModelInfo();
  var acpOpt = document.getElementById('opt-acpModel');
  if (acpOpt) acpOpt.style.display = model === 'copilot-acp' ? 'block' : 'none';
  if (_modeInitDone) {
    var needsLocal = model === 'lm-studio' ||
      (model === 'aig' && (localStorage.getItem('aigBackend') || '') === 'lmstudio');
    var currentMode = (document.getElementById('selDataMode') || {}).value || 'cloud';
    if (needsLocal && currentMode !== 'local') switchDataMode('local');
  }
}

function getACPModel() {
  var el = document.getElementById('selACPModel');
  return (el && el.value) ? el.value : '';
}

function getAIGModelPolicyMode() {
  var el = document.getElementById('selAIGModelPolicy');
    var value = el ? el.value : 'auto-balanced';
    return ['pinned', 'auto-balanced', 'auto-fast'].indexOf(value) >= 0 ? value : 'auto-balanced';
}

var __originalModelOptions = null;
var __modelBeforeLCARS = null;

function captureOriginalModelOptions() {
  if (__originalModelOptions) return;
  var sel = document.getElementById('selModel');
  if (!sel) return;
  __originalModelOptions = [];
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
  var allOpts = Array.from(sel.options);
  var hasCurrent = allOpts.some(function(o) { return o.value === currentValue; });
  sel.value = hasCurrent ? currentValue : (allOpts[0] ? allOpts[0].value : '');
  sel.dispatchEvent(new Event('change', { bubbles: true }));
}

function updateModelOptionsForTheme(theme) {
  captureOriginalModelOptions();
  var sel = document.getElementById('selModel');
  if (!sel) return;
  if (theme === 'lcars') {
    __modelBeforeLCARS = sel.value;
    var allowed = new Set(['aig']);
    var filtered = [];
    (__originalModelOptions || []).forEach(function(item) {
      if (item.type === 'optgroup') {
        var filteredOpts = item.group.options.filter(function(o) { return allowed.has(o.value); });
        if (filteredOpts.length) filtered.push({ type: 'optgroup', group: { label: item.group.label, options: filteredOpts } });
      } else if (allowed.has(item.value)) {
        filtered.push(item);
      }
    });
    if (filtered.length) setModelOptions(filtered);
  } else if (__originalModelOptions) {
    setModelOptions(__originalModelOptions);
    if (__modelBeforeLCARS) {
      var hasPrev = Array.from(sel.options).some(function(o) { return o.value === __modelBeforeLCARS; });
      if (hasPrev) {
        sel.value = __modelBeforeLCARS;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  }
  applyStandaloneSimplifications();
}