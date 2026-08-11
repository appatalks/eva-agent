// Native control facade for Eva's own renderer surfaces. This is deliberately
// allowlisted: it controls Eva without simulating pointer or keyboard input.
var EvaHarness = (function() {
  var aliases = {
    workspace: 'workspaces', coding_workspace: 'workspaces', coding_workspaces: 'workspaces',
    skill: 'skills',
    memories: 'memory', memory_inspector: 'memory',
    asset: 'assets', files: 'assets',
    chats: 'sessions', chat_history: 'sessions', history: 'sessions',
    console: 'terminal',
    preferences: 'settings', configuration: 'settings',
    model: 'models', model_settings: 'models',
    prompt: 'personality', prompts: 'personality', personality_settings: 'personality',
    goal: 'goals', background: 'background_jobs', jobs: 'background_jobs',
    cron: 'schedules', schedule: 'schedules', auth: 'accounts', account: 'accounts',
    tools: 'tools_memory', mcp: 'tools_memory', tools_and_memory: 'tools_memory',
    privacy: 'learning', learning_controls: 'learning',
    user_profile: 'profile', profiles: 'profile',
    voice_mode: 'voice', eva_voice: 'voice',
    agents: 'agent_operations', agent: 'agent_operations', agent_view: 'agent_operations'
  };

  function requireAction(condition, message, action) {
    if (!condition) throw new Error(message);
    return action();
  }

  function openSettings(tabName) {
    var button = document.getElementById('evaSettingsBtn');
    return requireAction(button, 'Settings API is unavailable.', function() {
      if (!document.body.classList.contains('settings-open')) button.click();
      if (!tabName) return;
      var tab = document.querySelector('.settings-tab[data-stab="' + tabName + '"]');
      if (!tab) throw new Error('Settings section is unavailable: ' + tabName);
      if (!tab.classList.contains('active')) tab.click();
    });
  }

  var navigation = {
    workspaces: function() { return requireAction(window.EvaWorkspaces && typeof EvaWorkspaces.openWorkbench === 'function', 'Workspaces API is unavailable.', function() { return EvaWorkspaces.openWorkbench(); }); },
    skills: function() { return requireAction(window.EvaSkills && typeof EvaSkills.open === 'function', 'Skills API is unavailable.', function() { return EvaSkills.open(); }); },
    memory: function() { return requireAction(window.EvaMemoryInspector && typeof EvaMemoryInspector.open === 'function', 'Memory API is unavailable.', function() { return EvaMemoryInspector.open(); }); },
    assets: function() { return requireAction(window.EvaAssets && typeof EvaAssets.open === 'function', 'Assets API is unavailable.', function() { return EvaAssets.open(); }); },
    sessions: function() {
      var panel = document.getElementById('sessionPanel');
      return requireAction(panel && typeof toggleSessionPanel === 'function', 'Sessions API is unavailable.', function() {
        if (panel.getAttribute('aria-hidden') === 'true') toggleSessionPanel();
      });
    },
    terminal: function() {
      var panel = document.getElementById('terminalPanel');
      return requireAction(panel && typeof toggleTerminalPanel === 'function', 'Terminal API is unavailable.', function() {
        if (panel.getAttribute('aria-hidden') === 'true') toggleTerminalPanel();
      });
    },
    settings: function() { return openSettings(); },
    models: function() { return openSettings('models'); },
    personality: function() { return openSettings('prompts'); },
    goals: function() { return openSettings('goals'); },
    background_jobs: function() { return openSettings('background'); },
    schedules: function() { return openSettings('cron'); },
    accounts: function() { return openSettings('auth'); },
    tools_memory: function() { return openSettings('mcp'); },
    learning: function() { return openSettings('learning'); },
    profile: function() {
      var button = document.getElementById('evaUserBtn');
      return requireAction(button, 'Profile API is unavailable.', function() { button.click(); });
    },
    voice: function() { return requireAction(typeof _vv !== 'undefined' && typeof openVoiceView === 'function', 'Voice API is unavailable.', function() { if (!_vv.open) openVoiceView(); }); },
    agent_operations: function() { return requireAction(window.EvaAgents && typeof EvaAgents.open === 'function', 'Agent Operations API is unavailable.', function() { return EvaAgents.open('agents'); }); }
  };

  function normalize(value) {
    return String(value || '').trim().toLowerCase().replace(/[ -]+/g, '_');
  }

  function resolveSurface(value) {
    var target = normalize(value);
    return aliases[target] || target;
  }

  function resolveNavigationRequest(value) {
    var phrase = String(value || '').trim().toLowerCase();
    var workspaceDescription = /\b(?:tell me|describe|list|summarize|summary|what|which)\b[\s\S]{0,48}\b(?:current\s+)?workspaces?\b|\bworkspaces?\b[\s\S]{0,32}\b(?:do i have|are available|can you access|current)\b/.test(phrase);
    if (workspaceDescription) return { action: 'describe_workspaces', target: 'workspaces', label: 'Workspaces' };
    var githubImperative = /^(?:please\s+)?(?:import|add|clone)\b|^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:import|add|clone)\b|^(?:i want you to|i'd like you to)\s+(?:import|add|clone)\b/.test(phrase);
    var githubImport = githubImperative && (/\b(?:github|repository|repo)\b/.test(phrase) || /https:\/\/github\.com\//.test(phrase));
    if (githubImport) {
      var repositoryUrl = (String(value || '').match(/https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?/i) || [])[0] || '';
      return { action: 'import_github', target: 'workspaces', label: 'GitHub Import', repositoryUrl: repositoryUrl };
    }
    if (!/\b(?:open|show|view|go to|switch to|switch over to|change to|navigate to|bring up|take me to)\b/.test(phrase)) return null;
    var targets = [
      { match: /\b(?:coding\s+)?workspaces?\b/, target: 'workspaces', label: 'Workspaces' },
      { match: /\bskills?\b/, target: 'skills', label: 'Skills' },
      { match: /\b(?:memory|memories|memory inspector)\b/, target: 'memory', label: 'Memory' },
      { match: /\b(?:assets?|files)\b/, target: 'assets', label: 'Assets' },
      { match: /\b(?:sessions?|chats?|chat history)\b/, target: 'sessions', label: 'Sessions' },
      { match: /\b(?:terminal|console)\b/, target: 'terminal', label: 'Terminal' },
      { match: /\b(?:models?|model settings)\b/, target: 'models', label: 'Models' },
      { match: /\b(?:personality|prompts?|prompt settings)\b/, target: 'personality', label: 'Personality' },
      { match: /\bgoals?\b/, target: 'goals', label: 'Goals' },
      { match: /\b(?:background(?: jobs?| activity)?)\b/, target: 'background_jobs', label: 'Background Jobs' },
      { match: /\b(?:schedules?|cron)\b/, target: 'schedules', label: 'Schedules' },
      { match: /\b(?:accounts?|authentication|auth(?: settings)?)\b/, target: 'accounts', label: 'Accounts' },
      { match: /\b(?:tools(?: and memory)?|tools & memory|mcp settings|mcp)\b/, target: 'tools_memory', label: 'Tools & Memory' },
      { match: /\b(?:learning|privacy(?: controls?)?)\b/, target: 'learning', label: 'Learning' },
      { match: /\b(?:user profile|profile picker|profiles?)\b/, target: 'profile', label: 'Profile' },
      { match: /\b(?:settings|preferences|configuration)\b/, target: 'settings', label: 'Settings' },
      { match: /\b(?:voice|voice mode|eva voice)\b/, target: 'voice', label: 'Eva Voice' },
      { match: /\b(?:agent operations|agents view|agents)\b/, target: 'agent_operations', label: 'Agent Operations' }
    ];
    return targets.find(function(item) { return item.match.test(phrase); }) || null;
  }

  function result(ok, label, message, data) {
    var response = { ok: !!ok, label: label || '', message: message || '' };
    if (data !== undefined) response.data = data;
    return response;
  }

  function navigate(target) {
    target = resolveSurface(target);
    var action = navigation[target];
    if (!action) return result(false, '', 'Unsupported Eva surface: ' + target);
    try {
      action();
      return result(true, target, 'Opened ' + target + '.');
    } catch (error) {
      return result(false, target, error && error.message ? error.message : 'Native navigation failed.');
    }
  }

  function refresh(target) {
    target = resolveSurface(target);
    var surface = {
      workspaces: window.EvaWorkspaces,
      skills: window.EvaSkills,
      memory: window.EvaMemoryInspector,
      assets: window.EvaAssets,
      agent_operations: window.EvaAgents
    }[target];
    if (!surface || typeof surface.refresh !== 'function') return result(false, target, 'This Eva surface cannot be refreshed.');
    try {
      surface.refresh();
      return result(true, target, 'Refreshed ' + target + '.');
    } catch (error) {
      return result(false, target, error && error.message ? error.message : 'Native refresh failed.');
    }
  }

  function execute(request, context) {
    request = request && typeof request === 'object' ? request : {};
    context = context && typeof context === 'object' ? context : {};
    var action = normalize(request.action);
    var modelAllowed = { navigate: true, refresh: true, describe_workspaces: true, inspect_form: true };
    if (context.source === 'model' && !modelAllowed[action]) {
      return result(false, action, 'This native action requires direct user interaction.');
    }
    if (action === 'navigate') return navigate(request.target);
    if (action === 'refresh') return refresh(request.target);
    if (action === 'describe_workspaces') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.describe !== 'function') return Promise.resolve(result(false, 'describe_workspaces', 'Workspaces description API is unavailable.'));
      return Promise.resolve(EvaWorkspaces.describe()).then(function(message) {
        return result(true, 'describe_workspaces', message);
      }).catch(function(error) {
        return result(false, 'describe_workspaces', error && error.message ? error.message : 'Workspaces could not be described.');
      });
    }
    if (action === 'inspect_form') {
      if (typeof evaTextPromptDescribe !== 'function') return result(false, 'inspect_form', 'Native form inspection is unavailable.');
      var schema = evaTextPromptDescribe();
      return result(true, 'inspect_form', schema.open ? 'Inspected the active native form.' : 'No native form is open.', schema);
    }
    if (action === 'set_field') {
      if (typeof evaTextPromptSetField !== 'function') return result(false, 'set_field', 'Native field control is unavailable.');
      var fieldResult = evaTextPromptSetField(request.field || request.field_id, request.value);
      if (!fieldResult.ok) return result(false, 'set_field', fieldResult.message, fieldResult);
      if (request.submit === true) {
        var submitted = evaTextPromptSubmit(fieldResult.field);
        return result(submitted.ok, 'set_field', submitted.message, submitted);
      }
      return result(true, 'set_field', fieldResult.message, fieldResult);
    }
    if (action === 'submit_form') {
      if (typeof evaTextPromptSubmit !== 'function') return result(false, 'submit_form', 'Native form submission is unavailable.');
      var submitResult = evaTextPromptSubmit(request.field || request.field_id || '');
      return result(submitResult.ok, 'submit_form', submitResult.message, submitResult);
    }
    if (action === 'cancel_form') {
      var cancelled = typeof evaTextPromptCancel === 'function' && evaTextPromptCancel();
      return result(cancelled, 'cancel_form', cancelled ? 'Cancelled the active native form.' : 'No native form is open.');
    }
    if (action === 'import_github') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.importGitHub !== 'function') return result(false, 'import_github', 'GitHub workspace import API is unavailable.');
      var opened = navigate('workspaces');
      if (!opened.ok) return opened;
      var repositoryUrl = request.repositoryUrl || request.repository_url || '';
      EvaWorkspaces.importGitHub(repositoryUrl);
      return result(true, 'import_github', repositoryUrl ? 'Started GitHub workspace import.' : 'Opened the GitHub workspace import prompt.');
    }
    if (action === 'new_chat') {
      var button = document.getElementById('evaNewChatBtn');
      if (!button) return result(false, 'new_chat', 'New chat is unavailable.');
      button.click();
      return result(true, 'new_chat', 'Started a new chat.');
    }
    if (action === 'voice_control') {
      var enabled = request.enabled !== false;
      if (typeof _vv === 'undefined' || typeof toggleCompactVoiceController !== 'function') return result(false, 'voice_control', 'Voice control is unavailable.');
      if (_vv.compactActive !== enabled) toggleCompactVoiceController();
      return result(true, 'voice_control', enabled ? 'Started Eva voice control.' : 'Stopped Eva voice control.');
    }
    return result(false, action, 'Unsupported Eva harness action.');
  }

  function capabilities() {
    return {
      actions: ['navigate', 'refresh', 'describe_workspaces', 'import_github', 'inspect_form', 'set_field', 'submit_form', 'cancel_form', 'new_chat', 'voice_control'],
      surfaces: Object.keys(navigation),
      aliases: Object.keys(aliases),
      nativeOnly: true
    };
  }

  function promptContract() {
    var contract = '\n\nNATIVE EVA HARNESS:\nFor Eva application controls, use [[EVA_HARNESS]]{"action":"navigate","target":"workspaces"}[[/EVA_HARNESS]] instead of browser or desktop automation. Navigate targets: workspaces, skills, memory, assets, sessions, terminal, settings, models, personality, goals, background_jobs, schedules, accounts, tools_memory, learning, profile, voice, and agent_operations. To list or summarize current coding workspaces, use [[EVA_HARNESS]]{"action":"describe_workspaces"}[[/EVA_HARNESS]]. Refresh targets: workspaces, skills, memory, assets, and agent_operations. To import GitHub into Workspaces, use [[EVA_HARNESS]]{"action":"import_github"}[[/EVA_HARNESS]] or include repository_url when known. Native forms support inspect_form, set_field, submit_form, and cancel_form. For the GitHub prompt use [[EVA_HARNESS]]{"action":"set_field","field":"github_repository_url","value":"https://github.com/owner/repository","submit":true}[[/EVA_HARNESS]]. For GitHub repository listing, use the configured GitHub tool output directly; do not run a local interpreter merely to reformat fetched JSON. Other actions: new_chat and voice_control with optional enabled:false. These actions control Eva directly; never use browser, screenshots, or desktop automation for those same Eva surfaces.';
    if (typeof evaTextPromptDescribe === 'function') {
      var schema = evaTextPromptDescribe();
      if (schema.open) contract += '\nCURRENT NATIVE FORM: ' + JSON.stringify(schema);
    }
    return contract;
  }

  return { execute: execute, navigate: navigate, refresh: refresh, capabilities: capabilities, promptContract: promptContract, resolveSurface: resolveSurface, resolveNavigationRequest: resolveNavigationRequest };
}());