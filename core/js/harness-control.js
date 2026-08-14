// Native control facade for Eva's own renderer surfaces. This is deliberately
// allowlisted: it controls Eva without simulating pointer or keyboard input.
var EvaHarness = (function() {
  var GITHUB_PULL_REQUEST_CONTEXT_KEY = 'eva_last_github_pull_request';
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

  var actionManifest = [
    { id: 'navigate', description: 'Open a native Eva surface. args: {target}. Read-only navigation.' },
    { id: 'refresh', description: 'Refresh a native surface. args: {target}. Read-only.' },
    { id: 'describe_workspaces', description: 'Open Workspaces and return the real workspace and active-run count. Read-only.' },
    { id: 'describe_assets', description: 'Open Assets and summarize generated and workspace files. Read-only.' },
    { id: 'describe_skills', description: 'Open Skills and summarize saved and active skills. Read-only.' },
    { id: 'describe_sessions', description: 'Open Sessions and summarize saved chat sessions. Read-only.' },
    { id: 'describe_agents', description: 'Open Agent Operations and summarize active and recent agents. Read-only.' },
    { id: 'describe_workspace_tools', description: 'Summarize enabled MCP tools for a workspace. args: {projectName?}. Read-only.' },
    { id: 'list_github_repositories', description: 'List the user-owned GitHub repositories after a direct user request. Read-only.' },
    { id: 'continue_github_repositories', description: 'Continue an explicit native GitHub repository listing. Read-only.' },
    { id: 'authorize_github', description: 'Open GitHub device authorization only for an explicit user request.' },
    { id: 'set_workspace_mcp_server', description: 'Enable or disable a named workspace MCP server only when the user explicitly asks.' },
    { id: 'verify_workspace_mcp_server', description: 'Start an isolated workspace check for a named MCP server after a direct request.' },
    { id: 'retry_workspace_run', description: 'Retry the named workspace run after a direct user request.' },
    { id: 'run_workspace_check', description: 'Start a requested workspace check or build.' },
    { id: 'run_repository_remediation', description: 'Start an explicitly requested repository remediation run.' },
    { id: 'import_github', description: 'Import an exact GitHub HTTPS URL only after a direct user request.' },
    { id: 'import_github_selection', description: 'Import a named repository selected from a native GitHub listing.' },
    { id: 'describe_github_pull_request', description: 'Inspect a GitHub pull request through authenticated gh. args: {number, repository?}. Read-only.' },
    { id: 'merge_github_pull_request', description: 'Verify and merge a pull request through authenticated gh only after a direct user request and typed MERGE confirmation.' },
    { id: 'delete_github_pull_request_branch', description: 'Delete only the verified head branch of a merged pull request after an explicit direct user request.' },
    { id: 'remove_workspace', description: 'Remove a named workspace only after a direct user request.' },
    { id: 'run_terminal_command', description: 'Submit an exact terminal command only after a direct user request.' },
    { id: 'type_terminal_command', description: 'Type an exact terminal command for review only after a direct user request.' },
    { id: 'plan_terminal_task', description: 'Plan and submit a direct user-requested terminal task.' },
    { id: 'consider_terminal_task', description: 'Consider a direct user request for terminal applicability.' },
    { id: 'inspect_form', description: 'Inspect the active native confirmation form. Read-only.' },
    { id: 'set_field', description: 'Set a field in the active native form.' },
    { id: 'submit_form', description: 'Submit the active native form.' },
    { id: 'cancel_form', description: 'Cancel the active native form.' },
    { id: 'new_chat', description: 'Start a new chat.' },
    { id: 'voice_control', description: 'Enable or disable Eva voice control.' }
  ];

  function normalize(value) {
    return String(value || '').trim().toLowerCase().replace(/[ -]+/g, '_');
  }

  function resolveSurface(value) {
    var target = normalize(value);
    return aliases[target] || target;
  }

  function repositoryRemediationRoute(rawPhrase) {
    var request = String(rawPhrase || '').trim();
    var githubRepository = request.match(/https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)(?:\/[A-Za-z0-9_./-]*)?/i);
    var securityRequest = /\b(?:dependabot|dependency|dependencies|codeql|security|alerts?)\b/i.test(request);
    var remediationAction = /\b(?:resolve|fix|remediate|address|update)\b/i.test(request);
    if (githubRepository && securityRequest && remediationAction) {
      return {
        action: 'run_repository_remediation', target: 'workspaces', label: 'Repository Remediation',
        repositoryName: githubRepository[1], objective: request.replace(/[.!?]+$/g, '').trim()
      };
    }
    var match = request.match(/^(?:please\s+)?(?:try\s+to\s+)?(?:resolve|fix|remediate|address|update)\b[\s\S]{0,180}\b(?:dependabot|dependency|dependencies|codeql|security|alerts?)\b[\s\S]{0,100}\b(?:in|for|on|with)\s+(?:the\s+)?([A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)?)(?:\s+(?:repo|repository))?[.!?]*$/i);
    if (!match) return null;
    return {
      action: 'run_repository_remediation', target: 'workspaces', label: 'Repository Remediation',
      repositoryName: String(match[1] || '').replace(/[.!?]+$/g, ''), objective: request.replace(/[.!?]+$/g, '').trim()
    };
  }

  function persistRemediationContext(context) {
    if (!context || !context.repositoryName || !context.objective) return;
    var value = {
      repositoryName: String(context.repositoryName).slice(0, 240),
      objective: String(context.objective).slice(0, 4000)
    };
    try { localStorage.setItem('eva_last_repository_remediation', JSON.stringify(value)); } catch (_) {}
    try {
      if (window.evaStandalone && typeof window.evaStandalone.workspaceRemediationContextSave === 'function') {
        Promise.resolve(window.evaStandalone.workspaceRemediationContextSave(value)).catch(function() {});
      }
    } catch (_) {}
  }

  function nativeRemediationContext() {
    try {
      if (!window.evaStandalone || typeof window.evaStandalone.workspaceRemediationContextLoad !== 'function') return null;
      var context = window.evaStandalone.workspaceRemediationContextLoad();
      if (context && /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)?$/.test(String(context.repositoryName || '')) && String(context.objective || '').trim()) {
        return { repositoryName: String(context.repositoryName), objective: String(context.objective).slice(0, 4000) };
      }
    } catch (_) {}
    return null;
  }

  function recentRemediationContext() {
    try {
      var messages = JSON.parse(localStorage.getItem('aigMessages') || '[]');
      if (!Array.isArray(messages)) return null;
      for (var index = messages.length - 1; index >= 0; index--) {
        var message = messages[index] || {};
        if (message.role !== 'user' || typeof message.content !== 'string') continue;
        var route = repositoryRemediationRoute(message.content);
        if (route) return { repositoryName: route.repositoryName, objective: route.objective };
      }
    } catch (_) {}
    try {
      var bubbles = document.querySelectorAll('.chat-bubble.user-bubble');
      for (var bubbleIndex = bubbles.length - 1; bubbleIndex >= 0; bubbleIndex--) {
        var visibleRoute = repositoryRemediationRoute(String(bubbles[bubbleIndex].textContent || '').replace(/^\s*You:\s*/i, ''));
        if (visibleRoute) return { repositoryName: visibleRoute.repositoryName, objective: visibleRoute.objective };
      }
    } catch (_) {}
    return null;
  }

  function pullRequestReference(value) {
    var text = String(value || '');
    var url = text.match(/https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/pull\/(\d+)/i);
    if (url) return { repository: normalizeGitHubRepository(url[1]), number: Number(url[2]) };
    var number = text.match(/\b(?:pull\s+request|pr)\s*#?\s*(\d+)\b/i);
    return number ? { repository: '', number: Number(number[1]) } : null;
  }

  function normalizeGitHubRepository(value) {
    var parts = String(value || '').trim().split('/');
    if (parts.length !== 2) return '';
    var owner = parts[0].toLowerCase() === 'apatox' ? 'appatalks' : parts[0];
    return /^[A-Za-z0-9_.-]+$/.test(owner) && /^[A-Za-z0-9_.-]+$/.test(parts[1]) ? owner + '/' + parts[1] : '';
  }

  function savedPullRequestContext() {
    try {
      var saved = JSON.parse(localStorage.getItem(GITHUB_PULL_REQUEST_CONTEXT_KEY) || 'null');
      if (saved && Number.isInteger(Number(saved.number)) && Number(saved.number) > 0 && normalizeGitHubRepository(saved.repository)) {
        return { number: Number(saved.number), repository: normalizeGitHubRepository(saved.repository) };
      }
    } catch (_) {}
    return null;
  }

  function persistPullRequestContext(context) {
    if (!context || !Number.isInteger(Number(context.number)) || !normalizeGitHubRepository(context.repository)) return;
    try {
      localStorage.setItem(GITHUB_PULL_REQUEST_CONTEXT_KEY, JSON.stringify({
        number: Number(context.number), repository: normalizeGitHubRepository(context.repository)
      }));
    } catch (_) {}
  }

  function recentPullRequestContext(rawPhrase) {
    var direct = pullRequestReference(rawPhrase);
    if (direct && direct.repository) return direct;
    var saved = savedPullRequestContext();
    if (saved && (!direct || saved.number === direct.number)) return saved;
    if (direct) return direct;
    var candidates = [];
    try {
      var messages = JSON.parse(localStorage.getItem('aigMessages') || '[]');
      if (Array.isArray(messages)) candidates = candidates.concat(messages.map(function(message) { return message && message.content; }));
    } catch (_) {}
    try {
      candidates = candidates.concat(Array.prototype.map.call(document.querySelectorAll('.chat-bubble'), function(bubble) { return bubble.textContent; }));
    } catch (_) {}
    for (var index = candidates.length - 1; index >= 0; index--) {
      var context = pullRequestReference(candidates[index]);
      if (context) return context;
    }
    return null;
  }

  function resolveNavigationRequest(value, options) {
    var rawPhrase = String(value || '').trim();
    rawPhrase = rawPhrase.replace(/^(?:(?:hi|hello|hey)\s+)?eva\s*[,!.:-]*\s*/i, '');
    if (/\b(?:github|repo|repository|pull\s+request|pr|issue|branch|workflow|release)\b/i.test(rawPhrase)) {
      rawPhrase = rawPhrase.replace(/\bapatox\b/gi, 'appatalks');
    }
    var phrase = rawPhrase.toLowerCase();
    var directUser = !!(options && options.directUser);
    var lastRemediation = null;
    try {
      var savedRemediation = JSON.parse(localStorage.getItem('eva_last_repository_remediation') || 'null');
      if (savedRemediation && typeof savedRemediation === 'object' &&
          /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)?$/.test(String(savedRemediation.repositoryName || '')) &&
          String(savedRemediation.objective || '').trim()) {
        lastRemediation = {
          repositoryName: String(savedRemediation.repositoryName),
          objective: String(savedRemediation.objective).slice(0, 4000)
        };
      }
    } catch (_) {}
    if (!lastRemediation) lastRemediation = nativeRemediationContext();
    var directPullRequest = pullRequestReference(rawPhrase);
    if (directPullRequest && !/\bmerge\b/i.test(rawPhrase)) {
      return {
        action: 'describe_github_pull_request', target: 'workspaces', label: 'GitHub Pull Request',
        number: directPullRequest.number, repository: directPullRequest.repository
      };
    }
    var workspaceDescription = /\b(?:tell me|describe|list|show|summarize|summary|what|which|count|inspect|check|review)\b[\s\S]{0,64}\b(?:current\s+)?workspaces?\b|\bworkspaces?\b[\s\S]{0,48}\b(?:do i have|are available|can you access|current|count|inspect|check|review)\b/.test(phrase);
    if (workspaceDescription) return { action: 'describe_workspaces', target: 'workspaces', label: 'Workspaces' };
    var assetsDescription = /\b(?:tell me|describe|list|show|summarize|summary|what|which|count|inspect|check|review)\b[\s\S]{0,64}\b(?:assets?|artifacts?|generated files?|workspace files?)\b|\b(?:assets?|artifacts?|generated files?|workspace files?)\b[\s\S]{0,48}\b(?:do i have|are available|can you access|current|count|inspect|check|review)\b/.test(phrase);
    if (assetsDescription) return { action: 'describe_assets', target: 'assets', label: 'Assets' };
    var skillsDescription = /\b(?:tell me|describe|list|show|summarize|summary|what|which|count|inspect|check|review)\b[\s\S]{0,64}\bskills?\b|\bskills?\b[\s\S]{0,48}\b(?:do i have|are available|can you access|current|count|inspect|check|review)\b/.test(phrase);
    if (skillsDescription) return { action: 'describe_skills', target: 'skills', label: 'Skills' };
    var sessionsDescription = /\b(?:tell me|describe|list|show|summarize|summary|what|which|count|inspect|check|review)\b[\s\S]{0,64}\b(?:sessions?|chat history|saved chats?)\b|\b(?:sessions?|chat history|saved chats?)\b[\s\S]{0,48}\b(?:do i have|are available|can you access|current|count|inspect|check|review)\b/.test(phrase);
    if (sessionsDescription) return { action: 'describe_sessions', target: 'sessions', label: 'Sessions' };
    var agentsDescription = /\b(?:tell me|describe|list|show|summarize|summary|what|which|count|inspect|check|review)\b[\s\S]{0,64}\b(?:agents?|agent operations|agent sessions?)\b|\b(?:agents?|agent operations|agent sessions?)\b[\s\S]{0,48}\b(?:do i have|are available|can you access|current|count|inspect|check|review)\b/.test(phrase);
    if (agentsDescription) return { action: 'describe_agents', target: 'agent_operations', label: 'Agent Operations' };
    var ownedRepositoryPhrase = '(?:my\\s+(?:github\\s+)?(?:repositories|repos)|owned\\s+(?:github\\s+)?(?:repositories|repos)|(?:github\\s+)?(?:repositories|repos)\\s+(?:that\\s+)?i\\s+own)';
    var githubListRequest = new RegExp(
      '^(?:(?:please\\s+)?(?:list|show|display|enumerate)|(?:can|could|would|will)\\s+you\\s+(?:please\\s+)?(?:list|show|display|enumerate)|(?:i\\s+want\\s+you\\s+to|i(?:\'d|\\s+would)\\s+like\\s+you\\s+to)\\s+(?:list|show|display|enumerate))\\s+' + ownedRepositoryPhrase + '(?:\\s+please)?[.!?]*$'
    ).test(phrase);
    var githubOwnedQuestion = new RegExp(
      '^(?:which|what)\\s+(?:are\\s+)?' + ownedRepositoryPhrase + '[!?]*$'
    ).test(phrase);
    var directOwnedRepositoryFollowUp = directUser && new RegExp(
      '^(?:(?:yes|yeah|correct|right)[,;:]?\\s+)?' + ownedRepositoryPhrase + '[.!?]*$'
    ).test(phrase) &&
      !/\b(?:don'?t|do not|never|without|unless|only after|wait|confirmation|if i consent|when i say)\b/.test(phrase);
    var directOwnedRepositoryList = directUser &&
      /\b(?:list|show|display|enumerate)\b/.test(phrase) &&
      /\b(?:my|owned)\b[\s\S]{0,28}\b(?:github\s+)?(?:repositories|repos)\b|\b(?:github\s+)?(?:repositories|repos)\b[\s\S]{0,28}\bi\s+(?:own|owned)\b/.test(phrase) &&
      !/\b(?:don'?t|do not|never|without|unless|only after|wait|confirmation|if i consent|when i say)\b/.test(phrase);
    var directAccountRepositoryList = directUser &&
      /\b(?:list|show|display|enumerate)\b/.test(phrase) &&
      /\bgithub\s+(?:repositories|repos)\b[\s\S]{0,32}\b(?:under|in|enter|on|for|from)\s+my\s+(?:github\s+)?account\b/.test(phrase) &&
      !/\b(?:don'?t|do not|never|without|unless|only after|wait|confirmation|if i consent|when i say)\b/.test(phrase);
    if (githubListRequest || githubOwnedQuestion || directOwnedRepositoryFollowUp || directOwnedRepositoryList || directAccountRepositoryList) {
      return { action: 'list_github_repositories', target: 'workspaces', label: 'GitHub Repositories' };
    }
    if (directUser) {
      var explicitRemediation = repositoryRemediationRoute(rawPhrase);
      if (explicitRemediation) {
        persistRemediationContext(explicitRemediation);
        return explicitRemediation;
      }
      var pullRequestFollowUp = /\b(?:create|open|raise|submit|publish)\b[\s\S]{0,32}\b(?:pull\s+request|pr)\b|\b(?:create|open|raise|submit|publish)\s+(?:it|that|the)\b[\s\S]{0,16}\bpr\b/i.test(rawPhrase);
      if (pullRequestFollowUp) {
        var pullRequestRemediation = lastRemediation || recentRemediationContext();
        if (pullRequestRemediation) {
          return {
            action: 'run_repository_remediation', target: 'workspaces', label: 'Repository Pull Request',
            repositoryName: pullRequestRemediation.repositoryName,
            objective: pullRequestRemediation.objective + '\n\nFollow-up: ' + rawPhrase.replace(/[.!?]+$/g, '').trim()
          };
        }
      }
      var workspaceContinuation = /\b(?:continue|resume|proceed)\b[\s\S]{0,96}\b(?:workspace|work|task|pull\s+request|pr)\b/i.test(rawPhrase);
      if (workspaceContinuation) {
        var continuedRemediation = lastRemediation || recentRemediationContext();
        if (continuedRemediation) {
          return {
            action: 'run_repository_remediation', target: 'workspaces', label: 'Repository Remediation',
            repositoryName: continuedRemediation.repositoryName,
            objective: continuedRemediation.objective + '\n\nFollow-up: ' + rawPhrase.replace(/[.!?]+$/g, '').trim()
          };
        }
      }
      var pullRequestMerge = /\bmerge\b/i.test(rawPhrase) && /\b(?:pull\s+request|pr|it|into\s+main)\b/i.test(rawPhrase);
      if (pullRequestMerge) {
        var pullRequest = recentPullRequestContext(rawPhrase);
        if (pullRequest) {
          return {
            action: 'merge_github_pull_request', target: 'workspaces', label: 'GitHub Pull Request Merge',
            number: pullRequest.number, repository: pullRequest.repository
          };
        }
      }
      var associatedBranchDelete = /\b(?:delete|remove)\b[\s\S]{0,48}\b(?:associated|source|pull\s+request|pr|merged)\b[\s\S]{0,32}\bbranch\b|\b(?:delete|remove)\s+(?:the\s+)?branch\b/i.test(rawPhrase);
      if (associatedBranchDelete) {
        var branchPullRequest = recentPullRequestContext(rawPhrase);
        return {
          action: 'delete_github_pull_request_branch', target: 'workspaces', label: 'GitHub Branch Deletion',
          number: branchPullRequest ? branchPullRequest.number : 0,
          repository: branchPullRequest ? branchPullRequest.repository : ''
        };
      }
      if (/\b(?:try|retry|rerun|resume|continue)\s+(?:again|it|that|the\s+(?:task|work|remediation))\b/i.test(rawPhrase)) {
        var retryRemediation = lastRemediation || recentRemediationContext();
        if (retryRemediation) {
          return {
            action: 'run_repository_remediation', target: 'workspaces', label: 'Repository Remediation',
            repositoryName: retryRemediation.repositoryName, objective: retryRemediation.objective
          };
        }
      }
      var workspaceRemovalVerb = '(?:(?:clean\\s*up)\\s+(?:and\\s+)?)?(?:remove|delete|forget)';
      var suffixedWorkspaceRemoval = rawPhrase.match(new RegExp('^(?:please\\s+)?' + workspaceRemovalVerb + '\\s+(?:the\\s+)?(.+?)\\s+(?:workspace|workspaces|project|projects|repository|repositories|repo|repos)(?:\\s*,[\\s\\S]*)?[.!?]*$', 'i'));
      var workspaceRemoval = suffixedWorkspaceRemoval || rawPhrase.match(new RegExp('^(?:please\\s+)?' + workspaceRemovalVerb + '\\s+(?:the\\s+)?(?:workspace|workspaces|project|projects|repository|repositories|repo|repos)\\s+(.+?)(?:\\s*,[\\s\\S]*)?[.!?]*$', 'i'));
      if (workspaceRemoval) {
        return {
          action: 'remove_workspace', target: 'workspaces', label: 'Workspace Removal',
          projectName: String(workspaceRemoval[1] || '').replace(/[.!?]+$/g, '').trim()
        };
      }
      var prefixedWorkspaceTools = rawPhrase.match(/^for\s+(?:the\s+)?(.+?)[,;:]?\s+(?:what(?:'s|\s+is)|which)\s+(?:is\s+|are\s+)?(?:the\s+)?enabled\s+(?:(?:workspace\s+)?(?:mcp\s+)?)?(?:tools?|servers?)[.!?]*$/i);
      var suffixedWorkspaceTools = rawPhrase.match(/^(?:what|which)\s+(?:(?:workspace\s+)?(?:mcp\s+)?)?(?:tools?|servers?)\s+(?:is|are)\s+enabled(?:\s+(?:for|in|on)\s+(?:the\s+)?(.+?))?[.!?]*$/i);
      if (prefixedWorkspaceTools || suffixedWorkspaceTools) {
        var workspaceToolsMatch = prefixedWorkspaceTools || suffixedWorkspaceTools;
        return {
          action: 'describe_workspace_tools', target: 'workspaces', label: 'Workspace Tools',
          projectName: String(workspaceToolsMatch[1] || '').replace(/[.!?]+$/g, '').trim()
        };
      }
      var workspaceCheckRequest = rawPhrase.match(/^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+)?(?:run|rerun|execute|start|perform|do)\s+(?:the\s+|a\s+|our\s+)?((?:smoke\s*tests?|test\s*suite|tests?|build|diagnostics?|checks?|lint|typecheck)(?:\s+checks?)?)(?:\s+(?:for|in|on)\s+(?:the\s+)?(?:selected|current|this)\s+(?:workspace|project|repository|repo))?[.!?]*$/i);
      if (workspaceCheckRequest) {
        return {
          action: 'run_workspace_check', target: 'workspaces', label: 'Workspace Check',
          objective: rawPhrase.replace(/[.!?]+$/g, '').trim()
        };
      }
      var workspaceRetry = rawPhrase.match(/^(?:please\s+)?(?:retry|resume|redispatch)\s+(?:the\s+)?(?:workspace|coding)\s+run(?:\s+([A-Za-z0-9-]+))?[.!?]*$/i);
      if (workspaceRetry) {
        return {
          action: 'retry_workspace_run', target: 'workspaces', label: 'Workspace Retry',
          runId: String(workspaceRetry[1] || '').replace(/[.!?]+$/g, '').trim()
        };
      }
      var githubContinuation = /^(?:please\s+)?(?:continue|proceed|go ahead)\b/i.test(rawPhrase) &&
        /\b(?:github|repositories|repos|repository|repo|import|clone)\b/i.test(rawPhrase) &&
        !/\b(?:don'?t|do not|never|without|unless|only after|wait|confirmation|if i consent|when i say)\b/.test(phrase);
      if (githubContinuation) {
        return { action: 'continue_github_repositories', target: 'workspaces', label: 'GitHub Repositories' };
      }
      var githubAuthorization = /\b(?:authorize|authenticate|sign\s*in|log\s*in|grant\s+(?:github\s+)?permissions?)\b[\s\S]{0,48}\bgithub\b|\bgithub\b[\s\S]{0,48}\b(?:authorize|authenticate|sign\s*in|log\s*in|permissions?)\b/i.test(rawPhrase);
      if (githubAuthorization && !/\b(?:don'?t|do not|never|without|unless|only after|wait|confirmation|if i consent|when i say)\b/.test(phrase)) {
        return { action: 'authorize_github', target: 'workspaces', label: 'GitHub Authorization' };
      }
      var workspaceMcpAction = rawPhrase.match(/^(?:please\s+)?(enable|disable|turn\s+on|turn\s+off)\s+(?:the\s+)?(?:workspace\s+)?mcp\s+(?:server\s+)?([A-Za-z0-9_.-]+)(?:\s+(?:for|in|on)\s+(.+?))?[.!?]*$/i);
      if (workspaceMcpAction) {
        return {
          action: 'set_workspace_mcp_server', target: 'workspaces', label: 'Workspace MCP Server',
          serverName: workspaceMcpAction[2], projectName: String(workspaceMcpAction[3] || '').replace(/[.!?]+$/g, '').trim(),
          enabled: /^(?:enable|turn\s+on)$/i.test(workspaceMcpAction[1])
        };
      }
      var workspaceMcpVerification = rawPhrase.match(/^(?:please\s+)?(?:verify|check|test)\s+(?:that\s+)?(?:the\s+)?(?:workspace\s+)?mcp\s+(?:server\s+)?([A-Za-z0-9_. -]+?)(?:\s+(?:for|in|on)\s+(.+?))?(?:\s+(?:is\s+)?(?:working|reachable|registered))?[.!?]*$/i)
        || rawPhrase.match(/(?:i\s+see\s+(?:we\s+have\s+)?(?:the\s+)?)?([A-Za-z0-9_. -]+?)\s+mcp\s+server\b[\s\S]{0,80}\b(?:verify|check|test)\b/i);
      if (workspaceMcpVerification) {
        return {
          action: 'verify_workspace_mcp_server', target: 'workspaces', label: 'Workspace MCP Server',
          serverName: workspaceMcpVerification[1], projectName: String(workspaceMcpVerification[2] || '').replace(/[.!?]+$/g, '').trim()
        };
      }
      var githubSelection = rawPhrase.match(/^(?:please\s+)?(?:import|add|clone)\s+(?:the\s+)?(?:github\s+)?(?:repository|repo)\s+(?:named\s+|called\s+)?([A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)?)[.!?]*$/i);
      if (githubSelection) return { action: 'import_github_selection', target: 'workspaces', label: 'GitHub Import', repositoryName: githubSelection[1].replace(/[.!?]+$/g, '') };
    }
    var githubImperative = /^(?:please\s+)?(?:import|add|clone)\b|^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:import|add|clone)\b|^(?:i want you to|i'd like you to)\s+(?:import|add|clone)\b/.test(phrase);
    var githubImport = githubImperative && (/\b(?:github|repository|repo)\b/.test(phrase) || /https:\/\/github\.com\//.test(phrase));
    if (githubImport) {
      var repositoryUrl = (String(value || '').match(/https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?/i) || [])[0] || '';
      return { action: 'import_github', target: 'workspaces', label: 'GitHub Import', repositoryUrl: repositoryUrl };
    }
    if (directUser) {
      var terminalTask = rawPhrase.match(/^(?:please\s+)?(type|run|execute)\s+(?:a|the)\s+command\s+(?:to|that)\s+(.+?)(?:\s+in\s+(?:the\s+)?terminal)?[.!?]*$/i);
      if (terminalTask) {
        var taskVerb = String(terminalTask[1] || '').toLowerCase();
        var objective = String(terminalTask[2] || '').trim();
        if (objective && !/[\r\n\0]/.test(objective) && objective.length <= 2000) {
          return { action: 'plan_terminal_task', target: 'terminal', label: 'Terminal', objective: objective, submit: taskVerb !== 'type' };
        }
      }
      var terminalCommand = rawPhrase.match(/^(?:please\s+)?(?:run|execute|type|enter)\s+(?:the\s+)?(?:command\s+)?(.+)$/i)
        || rawPhrase.match(/^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:run|execute|type|enter)\s+(?:the\s+)?(?:command\s+)?(.+)$/i)
        || rawPhrase.match(/^(?:please\s+)?use\s+(?:the\s+)?terminal\s+(?:to\s+)?(?:run|execute|type|enter)\s+(.+)$/i)
        || rawPhrase.match(/^(?:please\s+)?terminal\s+command\s*[:,-]?\s*(.+)$/i);
      if (terminalCommand) {
        var command = terminalCommand[1]
          .replace(/\s+in\s+(?:the\s+)?terminal[.!?]*$/i, '')
          .replace(/[.!?]+$/g, '')
          .trim();
        if (command && !/[\r\n\0]/.test(command) && command.length <= 8192) {
          var typeOnly = /^(?:please\s+)?type\b|^(?:can|could|would|will)\s+you\s+(?:please\s+)?type\b|^(?:please\s+)?use\s+(?:the\s+)?terminal\s+(?:to\s+)?type\b/i.test(rawPhrase);
          return { action: typeOnly ? 'type_terminal_command' : 'run_terminal_command', target: 'terminal', label: 'Terminal', command: command };
        }
      }
      var naturalTerminalTask = rawPhrase.match(/^(?:can|could|would|will)\s+you\s+(?:please\s+)?use\s+(?:the\s+)?(?:terminal|shell)\s+(?:to\s+)?(.+?)[.!?]*$/i)
        || rawPhrase.match(/^(?:please\s+)?use\s+(?:the\s+)?(?:terminal|shell)\s+(?:to\s+)?(.+?)[.!?]*$/i)
        || rawPhrase.match(/^(?:please\s+)?open\s+(?:the\s+)?terminal\s+and\s+(?:run|execute|do)\s+(.+?)[.!?]*$/i);
      if (naturalTerminalTask) {
        var naturalObjective = String(naturalTerminalTask[1] || '').trim();
        if (naturalObjective && !/[\r\n\0]/.test(naturalObjective) && naturalObjective.length <= 2000) {
          return { action: 'plan_terminal_task', target: 'terminal', label: 'Terminal', objective: naturalObjective, submit: true };
        }
      }
      var localInspectionTask = rawPhrase.match(/^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+)?((?:check|inspect|show|report|list)\b.+?)[.!?]*$/i);
      var localInspectionTarget = /\b(?:disk\s+(?:space|usage)|storage\s+(?:space|usage)|git\s+(?:status|changes|diff)|memory\s+usage|running\s+processes|process\s+list|current\s+(?:directory|folder)|directory\s+(?:contents|listing)|system\s+(?:status|information|info)|network\s+(?:status|interfaces?|connections?))\b/i;
      if (localInspectionTask && localInspectionTarget.test(localInspectionTask[1])) {
        var inspectionObjective = String(localInspectionTask[1] || '').trim();
        if (inspectionObjective && !/[\r\n\0]/.test(inspectionObjective) && inspectionObjective.length <= 2000) {
          return { action: 'plan_terminal_task', target: 'terminal', label: 'Terminal', objective: inspectionObjective, submit: true };
        }
      }
    }
    var navigationRequested = /\b(?:open|show|view|go to|switch to|switch over to|change to|navigate to|bring up|take me to)\b/.test(phrase);
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
    var navigationRoute = navigationRequested && targets.find(function(item) { return item.match.test(phrase); });
    if (navigationRoute) return navigationRoute;
    var genericQuestionOrRequest = /\?\s*$/.test(rawPhrase) || /^(?:please\b|(?:can|could|would|will)\s+you\b|(?:what|which|where|when|why|who|how|is|are|do|does|did|has|have)\b|(?:show|tell|find|check|inspect|report|list|search|calculate|compare|explain|summarize|create|update|remove|delete|move|copy|rename|install|build|test)\b)/i.test(rawPhrase);
    var explicitlyNegated = /\b(?:don'?t|do not|never|without|unless|only after|wait|when i say)\b/i.test(rawPhrase);
    if (directUser && genericQuestionOrRequest && !explicitlyNegated && !/[\r\n\0]/.test(rawPhrase) && rawPhrase.length <= 2000) {
      return { action: 'consider_terminal_task', target: 'terminal', label: 'Terminal', objective: rawPhrase, submit: true };
    }
    return null;
  }

  function githubRepositoryUrl(value) {
    var match = String(value || '').match(/https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?(?=$|[\s,.:;!?])/i);
    return match ? match[0] : '';
  }

  function isExactGitHubRepositoryUrl(value) {
    return /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\.git)?$/i.test(String(value || '').trim());
  }

  function result(ok, label, message, data) {
    var response = { ok: !!ok, label: label || '', message: message || '' };
    if (data !== undefined) response.data = data;
    return response;
  }

  function failureReason(error) {
    var message = String(error && error.message || error || '').toLowerCase();
    if (/cancel/.test(message)) return 'cancelled';
    if (/auth|credential|token|sign.?in/.test(message)) return 'authentication';
    if (/time.?out/.test(message)) return 'timeout';
    if (/unavailable|disabled|not connected|offline/.test(message)) return 'unavailable';
    return 'failed';
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
    var modelAllowed = { navigate: true, refresh: true, describe_workspaces: true, describe_assets: true, describe_skills: true, describe_sessions: true, describe_agents: true, describe_github_pull_request: true, inspect_form: true };
    var userRepositoryUrl = githubRepositoryUrl(context.userRequest);
    var requestedRepositoryUrl = String(request.repositoryUrl || request.repository_url || '').trim();
    var userNativeRoute = resolveNavigationRequest(context.userRequest || '', { directUser: true });
    var modelImport = action === 'import_github' &&
      userNativeRoute && userNativeRoute.action === 'import_github' &&
      !!userRepositoryUrl && isExactGitHubRepositoryUrl(requestedRepositoryUrl) && userRepositoryUrl === requestedRepositoryUrl;
    var modelGitHubAuthorization = action === 'authorize_github' && userNativeRoute && [
      'list_github_repositories', 'continue_github_repositories', 'import_github',
      'import_github_selection', 'authorize_github'
    ].indexOf(userNativeRoute.action) >= 0;
    var modelWorkspaceMcp = action === 'set_workspace_mcp_server' && userNativeRoute &&
      userNativeRoute.action === 'set_workspace_mcp_server' &&
      String(request.serverName || '').toLowerCase() === String(userNativeRoute.serverName || '').toLowerCase() &&
      request.enabled === userNativeRoute.enabled &&
      String(request.projectName || '').trim().toLowerCase() === String(userNativeRoute.projectName || '').trim().toLowerCase();
    var modelWorkspaceRetry = action === 'retry_workspace_run' && userNativeRoute &&
      userNativeRoute.action === 'retry_workspace_run' &&
      String(request.runId || '').trim() === String(userNativeRoute.runId || '').trim();
    var modelWorkspaceRemoval = action === 'remove_workspace' && userNativeRoute &&
      userNativeRoute.action === 'remove_workspace' &&
      String(request.projectName || '').trim().toLowerCase() === String(userNativeRoute.projectName || '').trim().toLowerCase();
    var modelRepositoryRemediation = action === 'run_repository_remediation' && userNativeRoute &&
      userNativeRoute.action === 'run_repository_remediation' &&
      String(request.repositoryName || '').toLowerCase() === String(userNativeRoute.repositoryName || '').toLowerCase() &&
      String(request.objective || '') === String(userNativeRoute.objective || '');
    if (context.source === 'model' && !modelAllowed[action] && !modelImport && !modelGitHubAuthorization && !modelWorkspaceMcp && !modelWorkspaceRetry && !modelWorkspaceRemoval && !modelRepositoryRemediation) {
      return result(false, action, 'This native action requires direct user interaction.');
    }
    if (action === 'navigate') return navigate(request.target);
    if (action === 'refresh') return refresh(request.target);
    if (action === 'describe_workspaces') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.describe !== 'function') return Promise.resolve(result(false, 'describe_workspaces', 'Workspaces description API is unavailable.'));
      var openedWorkspaces = navigate('workspaces');
      if (!openedWorkspaces.ok) return Promise.resolve(openedWorkspaces);
      return Promise.resolve(EvaWorkspaces.describe()).then(function(message) {
        return result(true, 'describe_workspaces', message);
      }).catch(function(error) {
        return result(false, 'describe_workspaces', error && error.message ? error.message : 'Workspaces could not be described.');
      });
    }
    if (action === 'describe_assets') {
      if (!window.EvaAssets || typeof EvaAssets.describe !== 'function') return Promise.resolve(result(false, 'describe_assets', 'Assets description API is unavailable.'));
      return Promise.resolve(EvaAssets.describe()).then(function(message) {
        var openedAssets = navigate('assets');
        if (!openedAssets.ok) return openedAssets;
        return result(true, 'describe_assets', message);
      }).catch(function(error) {
        return result(false, 'describe_assets', error && error.message ? error.message : 'Assets could not be described.');
      });
    }
    if (action === 'describe_skills') {
      if (!window.EvaSkills || typeof EvaSkills.describe !== 'function') return Promise.resolve(result(false, 'describe_skills', 'Skills description API is unavailable.'));
      return Promise.resolve(EvaSkills.describe()).then(function(message) {
        var openedSkills = navigate('skills');
        if (!openedSkills.ok) return openedSkills;
        return result(true, 'describe_skills', message);
      }).catch(function(error) {
        return result(false, 'describe_skills', error && error.message ? error.message : 'Skills could not be described.');
      });
    }
    if (action === 'describe_sessions') {
      var openedSessions = navigate('sessions');
      if (!openedSessions.ok) return Promise.resolve(openedSessions);
      if (typeof describeSavedSessions !== 'function') return Promise.resolve(result(false, 'describe_sessions', 'Sessions description API is unavailable.'));
      return Promise.resolve(describeSavedSessions()).then(function(message) {
        return result(true, 'describe_sessions', message);
      }).catch(function(error) {
        return result(false, 'describe_sessions', error && error.message ? error.message : 'Sessions could not be described.');
      });
    }
    if (action === 'describe_agents') {
      if (!window.EvaAgents || typeof EvaAgents.describe !== 'function') return Promise.resolve(result(false, 'describe_agents', 'Agent Operations description API is unavailable.'));
      return Promise.resolve(EvaAgents.describe()).then(function(message) {
        var openedAgents = navigate('agent_operations');
        if (!openedAgents.ok) return openedAgents;
        return result(true, 'describe_agents', message);
      }).catch(function(error) {
        return result(false, 'describe_agents', error && error.message ? error.message : 'Agent Operations could not be described.');
      });
    }
    if (action === 'describe_workspace_tools') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.describeProjectTools !== 'function') return Promise.resolve(result(false, 'describe_workspace_tools', 'Workspace tool status is unavailable.'));
      return Promise.resolve(EvaWorkspaces.describeProjectTools(request.projectName)).then(function(message) {
        return result(true, 'describe_workspace_tools', message, { outcome: 'completed' });
      }).catch(function(error) {
        return result(false, 'describe_workspace_tools', error && error.message ? error.message : 'Workspace tools could not be described.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'describe_github_pull_request') {
      if (!window.evaStandalone || typeof window.evaStandalone.githubViewPullRequest !== 'function') return Promise.resolve(result(false, 'describe_github_pull_request', 'Native GitHub CLI pull-request inspection is unavailable in this Eva build.'));
      var pullNumber = Number(request.number);
      if (!Number.isInteger(pullNumber) || pullNumber <= 0) return Promise.resolve(result(false, 'describe_github_pull_request', 'A pull request number is required for native inspection.'));
      return window.evaStandalone.githubViewPullRequest({
        number: pullNumber, repository: String(request.repository || '').trim()
      }).then(function(pull) {
        var checks = (pull && pull.checks || []).map(function(check) {
          return String(check.name || 'check') + ': ' + String(check.conclusion || 'PENDING');
        }).filter(Boolean);
        var summary = 'PR #' + pullNumber + (pull && pull.title ? ' "' + String(pull.title) + '"' : '') +
          ' is ' + String(pull && pull.state || 'UNKNOWN') +
          ' with merge state ' + String(pull && pull.mergeState || 'UNKNOWN') +
          (pull && pull.draft ? ' and is a draft' : '') + '.';
        if (checks.length) summary += ' Checks: ' + checks.join(', ') + '.';
        if (pull && pull.url) summary += ' ' + String(pull.url);
        var inferredRepository = String(pull && pull.url || '').replace(/^https:\/\/github\.com\//, '').replace(/\/pull\/\d+.*$/, '');
        persistPullRequestContext({ number: pullNumber, repository: String(request.repository || '') || inferredRepository });
        return result(true, 'describe_github_pull_request', summary, {
          outcome: 'completed', url: String(pull && pull.url || ''), state: String(pull && pull.state || ''), mergeState: String(pull && pull.mergeState || '')
        });
      }).catch(function(error) {
        return result(false, 'describe_github_pull_request', error && error.message ? error.message : 'Pull request inspection failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'merge_github_pull_request') {
      if (!window.evaStandalone || typeof window.evaStandalone.githubMergePullRequest !== 'function') return Promise.resolve(result(false, 'merge_github_pull_request', 'Native GitHub CLI merge is unavailable in this Eva build.'));
      var number = Number(request.number);
      if (!Number.isInteger(number) || number <= 0) return Promise.resolve(result(false, 'merge_github_pull_request', 'A pull request number is required to merge natively.'));
      if (typeof evaTextPrompt !== 'function') return Promise.resolve(result(false, 'merge_github_pull_request', 'Native merge confirmation is unavailable.'));
      return Promise.resolve(evaTextPrompt('Confirm merge PR #' + number, '', {
        maxLength: 5, placeholder: 'Type MERGE', kind: 'github_pr_merge'
      })).then(function(confirmation) {
        if (String(confirmation || '').trim().toUpperCase() !== 'MERGE') {
          return result(false, 'merge_github_pull_request', 'Pull request merge cancelled.', { outcome: 'cancelled' });
        }
        return window.evaStandalone.githubMergePullRequest({
          number: number, repository: String(request.repository || '').trim(), confirmation: 'MERGE'
        }).then(function(merged) {
          var url = String(merged && merged.url || '');
          return result(true, 'merge_github_pull_request', 'Merged pull request #' + number + (url ? ': ' + url : '.'), {
            outcome: 'completed', url: url, mergeCommit: String(merged && merged.mergeCommit || '')
          });
        });
      }).catch(function(error) {
        return result(false, 'merge_github_pull_request', error && error.message ? error.message : 'Pull request merge failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'delete_github_pull_request_branch') {
      if (!window.evaStandalone || typeof window.evaStandalone.githubDeletePullRequestBranch !== 'function') return Promise.resolve(result(false, 'delete_github_pull_request_branch', 'Native GitHub branch deletion is unavailable in this Eva build.'));
      var branchPullNumber = Number(request.number);
      if (!Number.isInteger(branchPullNumber) || branchPullNumber <= 0) return Promise.resolve(result(false, 'delete_github_pull_request_branch', 'Inspect the pull request URL first so Eva can identify the exact associated branch.'));
      return window.evaStandalone.githubDeletePullRequestBranch({
        number: branchPullNumber, repository: String(request.repository || '').trim()
      }).then(function(deleted) {
        return result(true, 'delete_github_pull_request_branch', 'Deleted branch ' + String(deleted.branch) + ' from ' + String(deleted.repository) + '.', {
          outcome: 'completed', branch: String(deleted.branch), repository: String(deleted.repository), url: String(deleted.url || '')
        });
      }).catch(function(error) {
        return result(false, 'delete_github_pull_request_branch', error && error.message ? error.message : 'Associated branch deletion failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'remove_workspace') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.removeProjectByName !== 'function') return Promise.resolve(result(false, 'remove_workspace', 'Workspace removal is unavailable.'));
      return Promise.resolve(EvaWorkspaces.removeProjectByName(request.projectName)).then(function(message) {
        return result(!/cancelled/i.test(message), 'remove_workspace', message, { outcome: /cancelled/i.test(message) ? 'cancelled' : 'completed' });
      }).catch(function(error) {
        return result(false, 'remove_workspace', error && error.message ? error.message : 'Workspace removal failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'list_github_repositories') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.listGitHubRepositories !== 'function') return Promise.resolve(result(false, 'list_github_repositories', 'GitHub repository listing API is unavailable.'));
      var openedWorkspaces = navigate('workspaces');
      if (!openedWorkspaces.ok) return Promise.resolve(openedWorkspaces);
      return Promise.resolve(EvaWorkspaces.listGitHubRepositories()).then(function(message) {
        return result(true, 'list_github_repositories', message, { outcome: 'completed' });
      }).catch(function(error) {
        return result(false, 'list_github_repositories', error && error.message ? error.message : 'GitHub repository listing failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'authorize_github') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.authorizeGitHub !== 'function') return Promise.resolve(result(false, 'authorize_github', 'GitHub CLI authorization is unavailable in this Eva build.'));
      var openedAuthorizationWorkspaces = navigate('workspaces');
      if (!openedAuthorizationWorkspaces.ok) return Promise.resolve(openedAuthorizationWorkspaces);
      return Promise.resolve(EvaWorkspaces.authorizeGitHub()).then(function() {
        return result(true, 'authorize_github', 'GitHub device authorization started.', { outcome: 'started' });
      }).catch(function(error) {
        return result(false, 'authorize_github', error && error.message ? error.message : 'GitHub authorization could not start.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'continue_github_repositories') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.continueGitHubRepositories !== 'function') return Promise.resolve(result(false, 'continue_github_repositories', 'GitHub repository continuation is unavailable in this Eva build.'));
      var openedContinuationWorkspaces = navigate('workspaces');
      if (!openedContinuationWorkspaces.ok) return Promise.resolve(openedContinuationWorkspaces);
      return Promise.resolve(EvaWorkspaces.continueGitHubRepositories()).then(function(message) {
        return result(true, 'continue_github_repositories', message, { outcome: 'completed' });
      }).catch(function(error) {
        return result(false, 'continue_github_repositories', error && error.message ? error.message : 'GitHub repository continuation failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'set_workspace_mcp_server') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.setProjectMcpServerByName !== 'function') return Promise.resolve(result(false, 'set_workspace_mcp_server', 'Workspace MCP controls are unavailable in this Eva build.'));
      var openedMcpWorkspaces = navigate('workspaces');
      if (!openedMcpWorkspaces.ok) return Promise.resolve(openedMcpWorkspaces);
      return Promise.resolve(EvaWorkspaces.setProjectMcpServerByName(request.serverName, request.enabled === true, request.projectName)).then(function(message) {
        return result(true, 'set_workspace_mcp_server', message, { outcome: 'completed' });
      }).catch(function(error) {
        return result(false, 'set_workspace_mcp_server', error && error.message ? error.message : 'Workspace MCP server update failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'retry_workspace_run') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.retryRun !== 'function') return Promise.resolve(result(false, 'retry_workspace_run', 'Workspace retry is unavailable in this Eva build.'));
      var openedRetryWorkspaces = navigate('workspaces');
      if (!openedRetryWorkspaces.ok) return Promise.resolve(openedRetryWorkspaces);
      return Promise.resolve(EvaWorkspaces.retryRun(request.runId)).then(function(message) {
        return result(true, 'retry_workspace_run', message, { outcome: 'started' });
      }).catch(function(error) {
        return result(false, 'retry_workspace_run', error && error.message ? error.message : 'Workspace retry failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'verify_workspace_mcp_server') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.verifyProjectMcpServerByName !== 'function') return Promise.resolve(result(false, 'verify_workspace_mcp_server', 'Workspace MCP verification is unavailable in this Eva build.'));
      var openedVerificationWorkspaces = navigate('workspaces');
      if (!openedVerificationWorkspaces.ok) return Promise.resolve(openedVerificationWorkspaces);
      return Promise.resolve(EvaWorkspaces.verifyProjectMcpServerByName(request.serverName, request.projectName)).then(function(message) {
        return result(true, 'verify_workspace_mcp_server', message, { outcome: 'started' });
      }).catch(function(error) {
        return result(false, 'verify_workspace_mcp_server', error && error.message ? error.message : 'Workspace MCP verification failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'run_workspace_check') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.runSelectedCheck !== 'function') return Promise.resolve(result(false, 'run_workspace_check', 'Workspace agent execution is unavailable.', { outcome: 'failed', reason: 'unavailable' }));
      var openedCheckWorkspaces = navigate('workspaces');
      if (!openedCheckWorkspaces.ok) return Promise.resolve(openedCheckWorkspaces);
      return Promise.resolve(EvaWorkspaces.runSelectedCheck(request.objective)).then(function(runResult) {
        return result(true, 'run_workspace_check', runResult.message, {
          outcome: runResult.outcome || 'started',
          reason: runResult.reason || '',
          runId: runResult.runId || ''
        });
      }).catch(function(error) {
        return result(false, 'run_workspace_check', error && error.message ? error.message : 'Workspace check could not start.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'run_repository_remediation') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.startRepositoryRemediation !== 'function') return Promise.resolve(result(false, 'run_repository_remediation', 'Repository remediation is unavailable in this Eva build.'));
      var openedRemediationWorkspaces = navigate('workspaces');
      if (!openedRemediationWorkspaces.ok) return Promise.resolve(openedRemediationWorkspaces);
      return Promise.resolve(EvaWorkspaces.startRepositoryRemediation(request.repositoryName, request.objective)).then(function(started) {
        persistRemediationContext({
          repositoryName: started.projectName || request.repositoryName,
          objective: request.objective
        });
        return result(true, 'run_repository_remediation', started.message, {
          outcome: started.dispatchError ? 'delayed' : 'started', runId: started.runId || '', projectName: started.projectName || ''
        });
      }).catch(function(error) {
        return result(false, 'run_repository_remediation', error && error.message ? error.message : 'Repository remediation could not start.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'import_github_selection') {
      if (!window.EvaWorkspaces || typeof EvaWorkspaces.importGitHubSelection !== 'function') return Promise.resolve(result(false, 'import_github_selection', 'GitHub repository selection API is unavailable.'));
      return Promise.resolve(EvaWorkspaces.importGitHubSelection(request.repositoryName)).then(function(project) {
        return result(!!project, 'import_github_selection', project ? 'GitHub workspace imported.' : 'GitHub workspace import cancelled.', { outcome: project ? 'imported' : 'cancelled' });
      }).catch(function(error) {
        return result(false, 'import_github_selection', error && error.message ? error.message : 'GitHub repository import failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'run_terminal_command') {
      if (typeof runEvaTerminalCommand !== 'function') return Promise.resolve(result(false, 'run_terminal_command', 'Native terminal command execution is unavailable.'));
      return Promise.resolve(runEvaTerminalCommand(request.command)).then(function() {
        return result(true, 'run_terminal_command', 'Command submitted to the terminal.', { outcome: 'submitted' });
      }).catch(function(error) {
        return result(false, 'run_terminal_command', error && error.message ? error.message : 'Terminal command could not be submitted.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'type_terminal_command') {
      if (typeof runEvaTerminalCommand !== 'function') return Promise.resolve(result(false, 'type_terminal_command', 'Native terminal command entry is unavailable.'));
      return Promise.resolve(runEvaTerminalCommand(request.command, false)).then(function() {
        return result(true, 'type_terminal_command', 'Command typed in the terminal for review.', { outcome: 'submitted' });
      }).catch(function(error) {
        return result(false, 'type_terminal_command', error && error.message ? error.message : 'Terminal command could not be typed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'plan_terminal_task') {
      if (typeof planEvaTerminalTask !== 'function') return Promise.resolve(result(false, 'plan_terminal_task', 'Native terminal task planning is unavailable.'));
      return Promise.resolve(planEvaTerminalTask(request.objective, request.submit !== false)).then(function(planned) {
        var submitted = planned && planned.submitted === true;
        return result(true, 'plan_terminal_task', submitted ? 'Planned command submitted to the terminal.' : 'Planned command typed in the terminal for review.', { outcome: 'submitted', reviewRequired: !!(planned && planned.reviewRequired) });
      }).catch(function(error) {
        return result(false, 'plan_terminal_task', error && error.message ? error.message : 'Terminal task planning failed.', { outcome: 'failed', reason: failureReason(error) });
      });
    }
    if (action === 'consider_terminal_task') {
      if (typeof planEvaTerminalTask !== 'function') return Promise.resolve(result(false, 'consider_terminal_task', 'Native terminal task planning is unavailable.'));
      return Promise.resolve(planEvaTerminalTask(request.objective, request.submit !== false, true)).then(function(planned) {
        if (planned && planned.declined === true) {
          return result(true, 'consider_terminal_task', 'This request does not need a terminal command.', { outcome: 'completed', declined: true });
        }
        var submitted = planned && planned.submitted === true;
        return result(true, 'consider_terminal_task', submitted ? 'Planned command submitted to the terminal.' : 'Planned command typed in the terminal for review.', { outcome: 'submitted', reviewRequired: !!(planned && planned.reviewRequired) });
      }).catch(function(error) {
        return result(false, 'consider_terminal_task', error && error.message ? error.message : 'Terminal task planning failed.', { outcome: 'failed', reason: failureReason(error) });
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
      return Promise.resolve(EvaWorkspaces.importGitHub(repositoryUrl)).then(function(project) {
        if (!project) return result(false, 'import_github', 'GitHub workspace import cancelled.', { outcome: 'cancelled' });
        return result(true, 'import_github', 'GitHub workspace imported.', { outcome: 'imported', project: project });
      }).catch(function(error) {
        return result(false, 'import_github', error && error.message ? error.message : 'GitHub workspace import failed.', { outcome: 'failed' });
      });
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
      actions: actionManifest.map(function(action) { return action.id; }),
      manifest: actionManifest.slice(),
      surfaces: Object.keys(navigation),
      aliases: Object.keys(aliases),
      nativeOnly: true
    };
  }

  function promptContract() {
    var contract = '\n\nNATIVE EVA HARNESS:\nFor Eva application controls, use [[EVA_HARNESS]]{"action":"navigate","target":"workspaces"}[[/EVA_HARNESS]] instead of browser or desktop automation. Navigate targets: workspaces, skills, memory, assets, sessions, terminal, settings, models, personality, goals, background_jobs, schedules, accounts, tools_memory, learning, profile, voice, and agent_operations. To list or summarize current coding workspaces, use [[EVA_HARNESS]]{"action":"describe_workspaces"}[[/EVA_HARNESS]]. To list the user\'s owned GitHub repositories, use [[EVA_HARNESS]]{"action":"list_github_repositories"}[[/EVA_HARNESS]] and present its returned URLs for user selection. To import a GitHub repository only after the user explicitly requests that import and its exact HTTPS URL is known, use [[EVA_HARNESS]]{"action":"import_github","repository_url":"https://github.com/owner/repository"}[[/EVA_HARNESS]]. When an explicit GitHub listing or import request needs repository authorization, use [[EVA_HARNESS]]{"action":"authorize_github"}[[/EVA_HARNESS]]; this opens a native device-code flow and never exposes a token. Workspace-local MCP servers come from each imported project\'s mcp.json and are isolated from global MCP configuration. When the user explicitly requests a named module, enable it with [[EVA_HARNESS]]{"action":"set_workspace_mcp_server","serverName":"<name>","enabled":true,"projectName":"<optional project>"}[[/EVA_HARNESS]]. When an explicit user request asks to retry a delayed coding run, use [[EVA_HARNESS]]{"action":"retry_workspace_run","runId":"<optional run id>"}[[/EVA_HARNESS]]. Do not use browser or desktop automation for these native workspace operations. Do not open an empty import form or use browser, terminal, or desktop control for GitHub repository listing/import. Terminal commands execute only from a direct user request and cannot be initiated by a model marker. Native forms support inspect_form, set_field, submit_form, and cancel_form for direct user interaction. Other actions: new_chat and voice_control with optional enabled:false. These actions control Eva directly; never use browser, screenshots, or desktop automation for those same Eva surfaces.';
    contract += '\nFor requests to open, count, inspect, list, or summarize Workspaces, use describe_workspaces. It opens the native Workspaces view and returns the real count. Never use browser or desktop automation for this.';
    contract += '\nAVAILABLE NATIVE ACTIONS:\n' + actionManifest.map(function(action) { return '- ' + action.id + ': ' + action.description; }).join('\n');
    if (typeof evaTextPromptDescribe === 'function') {
      var schema = evaTextPromptDescribe();
      if (schema.open) contract += '\nCURRENT NATIVE FORM: ' + JSON.stringify(schema);
    }
    return contract;
  }

  return { execute: execute, navigate: navigate, refresh: refresh, capabilities: capabilities, promptContract: promptContract, resolveSurface: resolveSurface, resolveNavigationRequest: resolveNavigationRequest };
}());