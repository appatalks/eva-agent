// copilot.js
// GitHub Copilot integration — two modes:
//   1. GitHub Models API (direct REST, requires PAT)
//   2. ACP Bridge (local server bridging Copilot CLI's Agent Client Protocol)
//
// Mode is determined by the selected model:
//   copilot-*     → GitHub Models API
//   copilot-acp   → ACP Bridge (uses copilot CLI via tools/acp_bridge.py)

// --- Helpers ---

function getCopilotMode(modelValue) {
  if (modelValue === 'copilot-acp') return 'acp';
  if (modelValue.indexOf('copilot-') === 0) return 'models-api';
  return 'models-api';
}

function isEvaStandalone() {
  return !!(typeof window !== 'undefined' && window.evaStandalone && window.evaStandalone.isStandalone);
}

function getStandaloneACPBridgeUrl() {
  if (!isEvaStandalone()) return '';
  return (window.evaStandalone.acpBaseUrl || '').trim();
}

function getACPBridgeUrl() {
  var standaloneUrl = getStandaloneACPBridgeUrl();
  if (standaloneUrl) return standaloneUrl;
  var el = document.getElementById('txtACPBridgeUrl');
  if (el && el.value.trim() && el.value.trim() !== 'http://localhost:8888') return el.value.trim();
  var stored = localStorage.getItem('acp_bridge_url');
  if (stored && stored !== 'http://localhost:8888') return stored;
  return 'http://localhost:8888';
}

// Auto-detect a reachable ACP bridge and cache the result
var _acpBridgeCache = null;
async function detectACPBridge() {
  if (_acpBridgeCache) return _acpBridgeCache;

  // Priority list: user-configured, same-origin server, localhost
  var candidates = [];
  var configured = getACPBridgeUrl();
  candidates.push(configured);

  if (!isEvaStandalone()) {
    // Try same host as the page (for when bridge runs on the web server)
    if (location.hostname && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
      candidates.push(location.protocol + '//' + location.hostname + ':8888');
      candidates.push('http://' + location.hostname + ':8888');
    }

    // Localhost fallback
    if (candidates.indexOf('http://localhost:8888') < 0) {
      candidates.push('http://localhost:8888');
    }
  }

  // Deduplicate
  var seen = {};
  candidates = candidates.filter(function(u) {
    if (seen[u]) return false;
    seen[u] = true;
    return true;
  });

  for (var i = 0; i < candidates.length; i++) {
    try {
      var resp = await fetch(candidates[i].replace(/\/+$/, '') + '/health', {
        method: 'GET',
        signal: AbortSignal.timeout(3000)
      });
      if (resp.ok) {
        var data = await resp.json();
        if (data.status === 'ok') {
          _acpBridgeCache = candidates[i];
          return candidates[i];
        }
      }
    } catch (e) {
      // Try next
    }
  }

  // Nothing found, return configured value anyway
  return configured;
}

// --- Main send function ---

async function copilotSend() {
  var txtMsg = document.getElementById('txtMsg');
  var txtOutput = document.getElementById('txtOutput');

  // Clean HTML artifacts from input
  txtMsg.innerHTML = txtMsg.innerHTML.replace(/<img\b[^>]*>/g, '');

  var sQuestion = txtMsg.innerHTML.replace(/<br>/g, '\n')
    .replace(/<div[^>]*>|<\/div>|&nbsp;|<span[^>]*>|<\/span>/gi, '');
  if (!sQuestion.trim()) {
    alert('Type in your question!');
    txtMsg.focus();
    return;
  }
  var signalContext = (typeof captureSignalDeliveryContext === 'function')
    ? captureSignalDeliveryContext(sQuestion)
    : null;
  var turnId = window._evaActiveAuditTurnId || (typeof evaCreateAuditTurnId === 'function' ? evaCreateAuditTurnId() : '');

  var selModel = document.getElementById('selModel');
  var mode = getCopilotMode(selModel.value);

  // Auth check — GitHub Models API requires PAT; ACP bridge does not (copilot CLI handles auth)
  if (mode === 'models-api') {
    var githubToken = getAuthKey('GITHUB_PAT');
    if (!githubToken) {
      txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">Error:</span> GitHub PAT not configured. Go to Settings \u2192 Auth and add your GitHub Personal Access Token.</div>';
      txtOutput.scrollTop = txtOutput.scrollHeight;
      setStatus('error', 'GitHub PAT not configured');
      return;
    }
  }

  // Display user message
  var safeUser = escapeHtml(sQuestion).replace(/\n/g, '<br>');
  txtOutput.innerHTML += '<div class="chat-bubble user-bubble"><span class="user">You:</span> ' + safeUser + '</div>';
  txtMsg.innerHTML = '';
  txtOutput.scrollTop = txtOutput.scrollHeight;

  // Build messages payload
  var storageKey = (mode === 'acp') ? 'copilotACPMessages' : 'copilotMessages';
  if (!localStorage.getItem(storageKey)) {
    var sysPrompt = (typeof getSystemPrompt === 'function') ? getSystemPrompt() : '';
    var initMessages = [
      { role: 'system', content: sysPrompt + ' When you are asked to show an image, instead describe the image with [Image of <Description>]. ' + (typeof dateContents !== 'undefined' ? dateContents : '') }
    ];
    localStorage.setItem(storageKey, JSON.stringify(initMessages));
  }

  var newMessages = [];
  if (lastResponse) {
    newMessages.push({ role: 'assistant', content: lastResponse.replace(/\n/g, ' ') });
  }
  newMessages.push({ role: 'user', content: sQuestion });

  // External data augmentation
  if (sQuestion.includes('weather') && typeof weatherContents !== 'undefined' && weatherContents) {
    newMessages.push({ role: 'user', content: "Today's " + weatherContents + ". " + sQuestion });
  }
  if (sQuestion.includes('news') && typeof newsContents !== 'undefined' && newsContents) {
    newMessages.push({ role: 'user', content: "Today's " + newsContents + ". " + sQuestion });
  }
  if ((sQuestion.includes('stock') || sQuestion.includes('markets') || sQuestion.includes('SPY')) && typeof marketContents !== 'undefined' && marketContents) {
    newMessages.push({ role: 'user', content: "Today's " + marketContents + " " + sQuestion });
  }
  if ((sQuestion.includes('solar') || sQuestion.includes('space weather')) && typeof solarContents !== 'undefined' && solarContents) {
    newMessages.push({ role: 'user', content: "Today's " + solarContents + " " + sQuestion });
  }

  var existingMessages = JSON.parse(localStorage.getItem(storageKey)) || [];
  existingMessages = existingMessages.concat(newMessages);
  localStorage.setItem(storageKey, JSON.stringify(existingMessages));

  // Route to the appropriate backend
  if (mode === 'acp') {
    await _copilotSendACP(existingMessages, sQuestion, txtOutput, storageKey, signalContext, turnId);
  } else {
    await _copilotSendModelsAPI(existingMessages, selModel.value, sQuestion, txtOutput, storageKey, signalContext, turnId);
  }
}

// --- GitHub Models API mode ---

async function _copilotSendModelsAPI(messages, modelValue, question, txtOutput, storageKey, signalContext, turnId) {
  var githubToken = getAuthKey('GITHUB_PAT');
  var model = modelValue.replace(/^copilot-/, '');
  var requestMessages = EvaPromptBudget.compactMessages(messages, {
    budget: 12000,
    recentTurns: 6
  }).messages;

  // --- Cognition: Fetch memory context from bridge and inject into system message ---
  var lastUserMsg = '';
  for (var i = requestMessages.length - 1; i >= 0; i--) {
    if (requestMessages[i].role === 'user') { lastUserMsg = requestMessages[i].content || ''; break; }
  }
  try {
    var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
    var contextSessionId = (typeof ensureActiveSessionId === 'function')
      ? ensureActiveSessionId() : ((typeof _activeSessionId === 'function') ? (_activeSessionId() || '') : '');
    var ctxResp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/context?message=' + encodeURIComponent(lastUserMsg) + '&session_id=' + encodeURIComponent(contextSessionId), {
      signal: AbortSignal.timeout(3000)
    });
    if (ctxResp.ok) {
      var ctxData = await ctxResp.json();
      if (ctxData.context && ctxData.cognition_enabled) {
        // Prepend memory context to the first system message, or insert one
        var injected = false;
        for (var j = 0; j < requestMessages.length; j++) {
          if (requestMessages[j].role === 'system' || requestMessages[j].role === 'developer') {
            requestMessages[j].content = ctxData.context + '\n\n' + requestMessages[j].content;
            injected = true;
            break;
          }
        }
        if (!injected) {
          requestMessages.unshift({ role: 'system', content: ctxData.context });
        }
      }
    }
  } catch (e) {
    // Bridge not available — continue without memory
  }

  var temp = (typeof getModelTemperature === 'function') ? getModelTemperature() : 0.7;
  var maxTok = (typeof getModelMaxTokens === 'function') ? getModelMaxTokens() : 16384;
  var requestBudget = EvaPromptBudget.compactMessages(requestMessages, {
    budget: 12000,
    recentTurns: 6
  });

  // Map short model names to GitHub Models API publisher/model format
  // See: https://github.com/marketplace/models/catalog
  var _modelMap = {
    'gpt-4o': 'openai/gpt-4o',
    'gpt-4o-mini': 'openai/gpt-4o-mini',
    'gpt-4.1': 'openai/gpt-4.1',
    'gpt-5.6-sol': 'openai/gpt-5.6-sol',
    'gpt-5.6-terra': 'openai/gpt-5.6-terra',
    'gpt-5.6-luna': 'openai/gpt-5.6-luna',
    'gpt-5': 'openai/gpt-5',
    'gpt-5-mini': 'openai/gpt-5-mini',
    'gpt-5-nano': 'openai/gpt-5-nano',
    'gpt-5-chat': 'openai/gpt-5-chat',
    'o3-mini': 'openai/o3-mini',
    'o3': 'openai/o3',
    'o4-mini': 'openai/o4-mini',
    'deepseek-r1': 'deepseek/DeepSeek-R1',
    'llama-4-maverick': 'meta/llama-4-maverick-17b-128e-instruct-fp8'
  };
  var apiModel = _modelMap[model] || ('openai/' + model);

  var payload = {
    model: apiModel,
    messages: requestBudget.messages,
    temperature: temp,
    max_tokens: maxTok
  };

  // Reasoning models: add reasoning_effort, remove temperature
  var reasoningModels = ['o3-mini', 'o4-mini', 'deepseek-r1', 'gpt-5', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'];
  if (reasoningModels.indexOf(model) >= 0) {
    var re = (typeof getReasoningEffortForModel === 'function') ? getReasoningEffortForModel(modelValue) : 'default';
    if (re !== 'default') payload.reasoning_effort = re;
    delete payload.temperature;
  }

  // GPT-5 family: use max_completion_tokens, remove temperature and stop
  if (model === 'gpt-5' || (model && model.indexOf('gpt-5.') === 0)) {
    delete payload.temperature;
  }

  setStatus('info', 'Sending to GitHub Models API (' + model + ')...');

  try {
    var url = 'https://models.github.ai/inference/chat/completions';
    if (typeof DEBUG_CORS !== 'undefined' && DEBUG_CORS && typeof DEBUG_PROXY_URL !== 'undefined' && DEBUG_PROXY_URL) {
      url = DEBUG_PROXY_URL + '/?target=' + encodeURIComponent(url);
    }

    var resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + githubToken,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });

    if (!resp.ok) {
      _copilotHandleHTTPError(resp, txtOutput);
      return;
    }

    var data = await resp.json();
    _copilotRenderResponse(data, txtOutput, model, question, signalContext, false, contextSessionId, turnId);

  } catch (err) {
    _copilotHandleFetchError(err, txtOutput);
  }
}

// --- ACP Bridge mode ---

async function _copilotSendACP(messages, question, txtOutput, storageKey, signalContext, turnId) {
  // Auto-detect bridge URL (tries configured, same-host, localhost)
  var bridgeUrl = await detectACPBridge();
  if (typeof watchACPPermissions === 'function') watchACPPermissions(190000);

  // Get selected ACP model (empty string = use CLI default)
  var acpModel = (typeof getACPModel === 'function') ? getACPModel() : '';
  var modelLabel = acpModel ? 'Copilot ACP (' + acpModel + ')' : 'Copilot ACP (default)';

  setStatus('info', 'Sending to ' + modelLabel + ' via ' + bridgeUrl + '...');

  var provisional = null;
  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/chat/completions';

    var payload = {
        messages: EvaPromptBudget.compactMessages(messages, { budget: 12000, recentTurns: 6 }).messages,
      model: 'copilot-acp',
      stream: true,
      session_id: (typeof ensureActiveSessionId === 'function')
        ? ensureActiveSessionId() : ((typeof _activeSessionId === 'function') ? (_activeSessionId() || '') : '')
    };
    if (acpModel) payload.acp_model = acpModel;
    payload.acp_auto_approve = true;
    var reasoningEffort = (typeof getReasoningEffortForModel === 'function') ? getReasoningEffortForModel('copilot-acp') : 'default';
    if (reasoningEffort !== 'default') payload.acp_reasoning_effort = reasoningEffort;

    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!resp.ok) {
      _copilotHandleHTTPError(resp, txtOutput);
      return;
    }

    var data = await readEvaStreamingResponse(resp, function (chunk) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      appendEvaStreamingChunk(provisional, chunk, txtOutput);
    });
    removeEvaStreamingBubble(provisional);
    await _copilotRenderResponse(data, txtOutput, modelLabel, question, signalContext, true, payload.session_id, turnId);

  } catch (err) {
    removeEvaStreamingBubble(provisional);
    var errorMessage = err.message || String(err);
    if (errorMessage.includes('Failed to fetch') || errorMessage.includes('NetworkError')) {
      errorMessage += ' \u2014 Is the ACP bridge server running? Start it with: python3 tools/acp_bridge.py';
    }
    _copilotHandleFetchError({ message: errorMessage }, txtOutput);
  }
}

// --- Shared response rendering ---

async function _copilotRenderResponse(data, txtOutput, modelLabel, userMessage, signalContext, reflectionHandledByBridge, reflectionSessionId, turnId) {
  var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';

  // Use unified renderer
  await renderEvaResponse(content, txtOutput, {
    signalAuthorized: !!(signalContext && signalContext.authorized),
    signalMessage: signalContext ? signalContext.message : '',
    signalRequest: userMessage,
    nativeRequest: userMessage,
    turnId: turnId,
    signalContext: signalContext
  });

  if (content) {
    lastResponse = content;
    var outputWithoutTags = txtOutput.innerText + '\n';
    masterOutput += outputWithoutTags;
    localStorage.setItem('masterOutput', masterOutput);
  }

  if (!(typeof reportCompletionTruncation === 'function' && reportCompletionTruncation(data))) {
    setStatus('info', 'Response received from ' + modelLabel);
  }

  // --- Cognition: Trigger post-response reflection via bridge ---
  if (!reflectionHandledByBridge && content && userMessage) {
    try {
      var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
      fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/reflect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_message: userMessage,
          assistant_message: content.substring(0, 500),
          model: modelLabel,
                  session_id: reflectionSessionId || ((typeof ensureActiveSessionId === 'function')
                    ? ensureActiveSessionId() : ((typeof _activeSessionId === 'function') ? (_activeSessionId() || '') : '')),
                  turn_id: turnId
        }),
        signal: AbortSignal.timeout(5000)
      }).catch(function() {}); // fire-and-forget
    } catch (e) {}
  }

  // Auto-speak
  var checkbox = document.getElementById('autoSpeak');
  if (checkbox && checkbox.checked) {
    speakText();
    var audio = document.getElementById('audioPlayback');
    if (audio) audio.setAttribute('autoplay', true);
  }
}

// --- Error handling ---

async function _copilotHandleHTTPError(resp, txtOutput) {
  var errText = await resp.text();
  var errMsg = 'Error ' + resp.status;
  try {
    var errJson = JSON.parse(errText);
    errMsg += ': ' + (errJson.error ? (errJson.error.message || errJson.error) : (errJson.message || errText));
  } catch (e) {
    errMsg += ': ' + errText;
  }
  txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">' + escapeHtml(errMsg) + '</span></div>';
  txtOutput.scrollTop = txtOutput.scrollHeight;
  setStatus('error', errMsg);
}

function _copilotHandleFetchError(err, txtOutput) {
  console.error('Copilot error:', err);
  var errorMessage = err.message || String(err);
  if (errorMessage.includes('Failed to fetch') || errorMessage.includes('NetworkError') || errorMessage.includes('CORS')) {
    if (!errorMessage.includes('ACP bridge')) {
      errorMessage += ' \u2014 This may be a CORS issue. Configure DEBUG_CORS and DEBUG_PROXY_URL in config.json, or use a CORS proxy.';
    }
  }
  txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">Error:</span> ' + escapeHtml(errorMessage) + '</div>';
  txtOutput.scrollTop = txtOutput.scrollHeight;
  setStatus('error', errorMessage);
}

// --- MCP Configuration ---

// Populate the Settings MCP form fields from a saved config object. Shared by the
// DOMContentLoaded loader and the bridge-restore path so both stay in sync.
function populateMCPForm(cfg) {
  if (!cfg || typeof cfg !== 'object') return;
  var azureCheck = document.getElementById('mcpAzure');
  var githubCheck = document.getElementById('mcpGitHub');
  if (azureCheck) azureCheck.checked = !!cfg['azure-mcp-server'];
  if (githubCheck) githubCheck.checked = !!cfg['github-mcp-server'];
  var kustoCheckL = document.getElementById('mcpKusto');
  if (kustoCheckL) kustoCheckL.checked = !!cfg['kusto-mcp-server'];
  if (cfg['kusto-mcp-server'] && cfg['kusto-mcp-server'].env) {
    var kc = document.getElementById('mcpKustoCluster');
    var kd = document.getElementById('mcpKustoDatabase');
    if (kc && cfg['kusto-mcp-server'].env.KUSTO_CLUSTER_URL) kc.value = cfg['kusto-mcp-server'].env.KUSTO_CLUSTER_URL;
    if (kd && cfg['kusto-mcp-server'].env.KUSTO_DATABASE) kd.value = cfg['kusto-mcp-server'].env.KUSTO_DATABASE;
  }
  var kustoConfig = document.getElementById('mcpKustoConfig');
  if (kustoCheckL && kustoConfig) {
    kustoConfig.style.display = kustoCheckL.checked ? 'block' : 'none';
  }
  var cuCheck = document.getElementById('mcpComputerUse');
  if (cuCheck) cuCheck.checked = !!cfg['computer-use-linux'];
}

function forgetMissingMCPSelections(config, unavailable) {
  var retained = Object.assign({}, config || {});
  var changed = false;
  Object.keys(unavailable || {}).forEach(function(name) {
    if (unavailable[name] === 'command_not_found' && retained[name]) {
      delete retained[name];
      changed = true;
    }
  });
  if (changed) {
    localStorage.setItem('mcp_config', JSON.stringify(retained));
    populateMCPForm(retained);
  }
  return retained;
}

// Re-apply the saved MCP config to a freshly started bridge.
// The bridge is a new process on every launch with no MCP servers configured,
// so without this the user would have to re-Configure Kusto/Azure/GitHub MCP
// after each restart. Reads the persisted config directly from localStorage so
// it does not depend on the Settings form fields being populated yet.
var _lastAutoAppliedMCPPat = null;
var _autoApplyMCPQueue = Promise.resolve();
function autoApplySavedMCPConfig() {
  var requestedPat = (typeof getAuthKey === 'function') ? getAuthKey('GITHUB_PAT') : '';
  _autoApplyMCPQueue = _autoApplyMCPQueue.catch(function() {}).then(function() {
    return _applySavedMCPConfig(requestedPat);
  });
  return _autoApplyMCPQueue;
}

async function _applySavedMCPConfig(githubPat) {
  if (typeof evaAuthReady !== 'undefined' && evaAuthReady) {
    await evaAuthReady;
    githubPat = (typeof getAuthKey === 'function') ? getAuthKey('GITHUB_PAT') : githubPat;
  }
  var saved;
  try {
    saved = JSON.parse(localStorage.getItem('mcp_config') || 'null');
  } catch (e) {
    saved = null;
  }

  // localStorage lives under the Electron file:// origin and is wiped across some
  // app rebuilds/restarts. When it is empty, restore from the bridge's persisted
  // copy (secrets stripped) so the user does not have to reconfigure MCP.
  if (!saved || typeof saved !== 'object' || Object.keys(saved).length === 0) {
    try {
      var bridgeForRestore = await detectACPBridge();
      var cfgResp = await fetch(bridgeForRestore.replace(/\/+$/, '') + '/v1/mcp/config');
      if (cfgResp.ok) {
        var cfgData = await cfgResp.json();
        var restored = cfgData && cfgData.mcp_servers;
        if (restored && typeof restored === 'object' && Object.keys(restored).length > 0) {
          saved = restored;
          localStorage.setItem('mcp_config', JSON.stringify(saved));
          populateMCPForm(saved);
          try {
            var _k = restored['kusto-mcp-server'];
            if (_k && _k.env && _k.env.KUSTO_CLUSTER_URL) {
              localStorage.setItem('eva_standalone_first_run_done', '1');
            }
          } catch (_e) {}
        }
      }
    } catch (e) {
      // Bridge not ready or no persisted config; nothing to restore.
    }
  }

  if (!saved || typeof saved !== 'object' || Object.keys(saved).length === 0) return;
  if (_lastAutoAppliedMCPPat === githubPat) return;

  // The standalone window only loads after the bridge reports healthy, but allow
  // a few short retries in case MCP server startup lags the health check.
  for (var attempt = 0; attempt < 3; attempt++) {
    try {
      var bridgeUrl = await detectACPBridge();
      var resp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/mcp/configure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mcp_servers: saved,
          github_pat: githubPat
        })
      });
      if (resp.ok) {
        _lastAutoAppliedMCPPat = githubPat;
        var data = await resp.json();
        saved = forgetMissingMCPSelections(saved, data.unavailable_servers);
        var unavailable = Object.keys(data.unavailable_servers || {});
        setStatus(unavailable.length ? 'error' : 'info', 'MCP restored: ' + ((data.active_servers || []).join(', ') || 'none') + (unavailable.length ? '. Unavailable: ' + unavailable.join(', ') : ''));
        if (typeof refreshMCPStatus === 'function') refreshMCPStatus();
        return;
      }
    } catch (e) {
      // Bridge not ready yet; wait briefly and retry.
    }
    await new Promise(function (r) { setTimeout(r, 1500); });
  }
}

if (isEvaStandalone() && window.evaStandalone && typeof window.evaStandalone.onGitHubAuthComplete === 'function') {
  window.evaStandalone.onGitHubAuthComplete(function() {
    autoApplySavedMCPConfig().catch(function() {});
  });
}

async function applyMCPConfig() {
  var bridgeUrl = await detectACPBridge();
  var mcpServers = {};

  // Azure MCP
  var azureCheck = document.getElementById('mcpAzure');
  if (azureCheck && azureCheck.checked) {
    mcpServers['azure-mcp-server'] = {
      command: 'npx',
      args: ['-y', '@azure/mcp@3.0.0-beta.31', 'server', 'start'],
      env: { AZURE_MCP_COLLECT_TELEMETRY: 'false' }
    };
  }

  // GitHub MCP
  var githubCheck = document.getElementById('mcpGitHub');
  if (githubCheck && githubCheck.checked) {
    mcpServers['github-mcp-server'] = {
      command: 'docker',
      args: ['run', '-i', '--rm', '-e', 'GITHUB_PERSONAL_ACCESS_TOKEN', 'ghcr.io/github/github-mcp-server'],
      env: { _useGitHubPAT: true }  // flag — bridge resolves PAT server-side
    };
  }

  // Kusto MCP
  var kustoCheck = document.getElementById('mcpKusto');
  if (kustoCheck && kustoCheck.checked) {
    var kustoEnv = {};
    var clusterEl = document.getElementById('mcpKustoCluster');
    var dbEl = document.getElementById('mcpKustoDatabase');
    if (clusterEl && clusterEl.value.trim()) kustoEnv.KUSTO_CLUSTER_URL = clusterEl.value.trim();
    if (dbEl && dbEl.value.trim()) kustoEnv.KUSTO_DATABASE = dbEl.value.trim();
    if (typeof isEvaStandalone === 'function' && isEvaStandalone()) kustoEnv.KUSTO_DATABASE_LOCKED = '1';
    mcpServers['kusto-mcp-server'] = {
      command: 'python3',
      args: ['tools/kusto_mcp.py'],
      env: kustoEnv
    };
  }

  // Computer Use Linux MCP (AT-SPI desktop control)
  var cuCheck = document.getElementById('mcpComputerUse');
  if (cuCheck && cuCheck.checked) {
    mcpServers['computer-use-linux'] = {
      command: 'computer-use-linux',
      args: ['mcp']
    };
  }

  // Save to localStorage
  localStorage.setItem('mcp_config', JSON.stringify(mcpServers));

  // Send to bridge
  setStatus('info', 'Configuring MCP servers...');
  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/mcp/configure';
    var configBody = { mcp_servers: mcpServers };
    // Include GitHub PAT so the bridge can inject it into the MCP server env
    var ghPat = (typeof getAuthKey === 'function') ? getAuthKey('GITHUB_PAT') : '';
    if (ghPat) configBody.github_pat = ghPat;
    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configBody)
    });
    var data = await resp.json();
    if (resp.ok) {
      mcpServers = forgetMissingMCPSelections(mcpServers, data.unavailable_servers);
      var unavailable = Object.keys(data.unavailable_servers || {});
      setStatus(unavailable.length ? 'error' : 'info', 'MCP configured: ' + ((data.active_servers || []).join(', ') || 'none') + (unavailable.length ? '. Unavailable: ' + unavailable.join(', ') : ''));
      refreshMCPStatus();
      return { ok: true, data: data, bridgeUrl: bridgeUrl, mcpServers: mcpServers };
    } else {
      setStatus('error', 'MCP config error: ' + (data.error ? data.error.message : 'Unknown'));
      return { ok: false, data: data, bridgeUrl: bridgeUrl, mcpServers: mcpServers };
    }
  } catch (e) {
    setStatus('error', 'MCP config failed: ' + e.message + ' — Is the ACP bridge running?');
    return { ok: false, error: e, bridgeUrl: bridgeUrl, mcpServers: mcpServers };
  }
}

function getKustoSeedValues() {
  var clusterEl = document.getElementById('mcpKustoCluster');
  var databaseEl = document.getElementById('mcpKustoDatabase');
  return {
    clusterUrl: clusterEl ? clusterEl.value.trim() : '',
    database: databaseEl ? databaseEl.value.trim() : ''
  };
}

function setKustoSeedStatus(type, text) {
  var statusEl = document.getElementById('mcpSeedStatus');
  if (statusEl) {
    statusEl.textContent = text || '';
    statusEl.setAttribute('data-status', type || 'info');
  }
  if (text) setStatus(type === 'error' ? 'error' : 'info', text);
}

function setArtifactPurgeStatus(type, text) {
  var statusEl = document.getElementById('mcpPurgeArtifactsStatus');
  if (statusEl) {
    statusEl.textContent = text || '';
    statusEl.setAttribute('data-status', type || 'info');
  }
  if (text) setStatus(type === 'error' ? 'error' : 'info', text);
}

function updateKustoSeedButtonState() {
  var values = getKustoSeedValues();
  var ready = !!(values.clusterUrl && values.database);
  var button = document.getElementById('mcpSeedButton');
  if (button) button.disabled = !ready;
  var ensureButton = document.getElementById('mcpEnsureTablesButton');
  if (ensureButton) ensureButton.disabled = !ready;
}

async function seedEvaSchema(clusterUrl, database, alreadyConfirmed) {
  clusterUrl = (clusterUrl || '').trim();
  database = (database || '').trim();
  if (!clusterUrl || !database) {
    setKustoSeedStatus('error', 'Cluster URL and database are required before seeding.');
    return { ok: false, error: 'missing_inputs' };
  }
  if (!alreadyConfirmed) {
    var confirmed = confirm('Seed Eva schema into ' + database + '? This writes starter tables and rows. Running it again can duplicate inline seed rows.');
    if (!confirmed) return { ok: false, skipped: true };
  }

  var button = document.getElementById('mcpSeedButton');
  if (button) button.disabled = true;
  setKustoSeedStatus('info', 'Seeding Eva schema...');

  try {
    var bridgeUrl = await detectACPBridge();
    var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/kusto/seed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cluster_url: clusterUrl, database: database })
    });
    var data = await response.json();
    if (!response.ok || !data.ok) {
      var errors = (data && data.errors && data.errors.length) ? data.errors.slice(0, 3).join(' ') : (data.error && data.error.message ? data.error.message : 'Unknown seed error');
      setKustoSeedStatus('error', 'Schema seed failed: ' + errors);
      return { ok: false, data: data };
    }
    var message = 'Schema seed complete: ' + data.applied + ' applied, ' + data.failed + ' failed.';
    if (data.warning) message += ' ' + data.warning;
    setKustoSeedStatus('info', message);
    return { ok: true, data: data };
  } catch (error) {
    setKustoSeedStatus('error', 'Schema seed failed: ' + error.message);
    return { ok: false, error: error };
  } finally {
    updateKustoSeedButtonState();
  }
}

async function ensureEvaTables(clusterUrl, database) {
  clusterUrl = (clusterUrl || '').trim();
  database = (database || '').trim();
  if (!clusterUrl || !database) {
    setKustoSeedStatus('error', 'Cluster URL and database are required before creating tables.');
    return { ok: false, error: 'missing_inputs' };
  }

  var button = document.getElementById('mcpEnsureTablesButton');
  if (button) button.disabled = true;
  setKustoSeedStatus('info', 'Creating any missing tables...');

  try {
    var bridgeUrl = await detectACPBridge();
    var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/kusto/seed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cluster_url: clusterUrl, database: database, schema_only: true })
    });
    var data = await response.json();
    if (!response.ok || !data.ok) {
      var errors = (data && data.errors && data.errors.length) ? data.errors.slice(0, 3).join(' ') : (data.error && data.error.message ? data.error.message : 'Unknown error');
      setKustoSeedStatus('error', 'Table creation failed: ' + errors);
      return { ok: false, data: data };
    }
    setKustoSeedStatus('info', 'Tables ready: ' + data.applied + ' verified, ' + data.failed + ' failed. Existing data was left untouched.');
    return { ok: true, data: data };
  } catch (error) {
    setKustoSeedStatus('error', 'Table creation failed: ' + error.message);
    return { ok: false, error: error };
  } finally {
    updateKustoSeedButtonState();
  }
}

async function purgeArtifactsFromSettings() {
  if (!confirm('Delete all generated artifacts? This cannot be undone.')) return { ok: false, skipped: true };

  var button = document.getElementById('mcpPurgeArtifactsButton');
  if (button) button.disabled = true;
  setArtifactPurgeStatus('info', 'Purging artifacts...');

  try {
    var bridgeUrl = await detectACPBridge();
    var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/files/purge', {
      method: 'POST',
      body: ''
    });
    var data = await response.json();
    if (!response.ok || data.status !== 'ok') {
      var message = data && data.error && data.error.message ? data.error.message : 'Artifact purge failed';
      setArtifactPurgeStatus('error', message);
      return { ok: false, data: data };
    }
    var purged = typeof data.purged === 'number' ? data.purged : 0;
    setArtifactPurgeStatus('info', 'Purged ' + purged + ' artifacts.');
    return { ok: true, data: data };
  } catch (error) {
    setArtifactPurgeStatus('error', 'Artifact purge failed: ' + error.message);
    return { ok: false, error: error };
  } finally {
    if (button) button.disabled = false;
  }
}

async function seedEvaSchemaFromSettings() {
  var values = getKustoSeedValues();
  return seedEvaSchema(values.clusterUrl, values.database, false);
}

async function ensureEvaTablesFromSettings() {
  var values = getKustoSeedValues();
  return ensureEvaTables(values.clusterUrl, values.database);
}

async function refreshMCPStatus() {
  var statusEl = document.getElementById('mcpStatus');
  if (!statusEl) return;

  var bridgeUrl = getACPBridgeUrl();
  try {
    var resp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/mcp', {
      signal: AbortSignal.timeout(3000)
    });
    if (resp.ok) {
      var data = await resp.json();
      var active = data.active || [];
      var unavailableState = data.unavailable || {};
      var unavailable = Object.keys(unavailableState);
      var saved = {};
      try {
        saved = JSON.parse(localStorage.getItem('mcp_config') || '{}') || {};
      } catch (e) {}
      saved = forgetMissingMCPSelections(saved, unavailableState);
      if (active.length > 0) {
        statusEl.innerHTML = '<strong>Active MCP Servers:</strong> ' + active.map(function(s) { return '<span class="mcp-badge">' + escapeHtml(s) + '</span>'; }).join(' ');
      } else {
        statusEl.innerHTML = '<em>No MCP servers active</em>';
      }
      if (unavailable.length) statusEl.innerHTML += '<div><strong>Unavailable:</strong> ' + unavailable.map(escapeHtml).join(', ') + '</div>';
      var presetStates = {
        mcpAzure: 'azure-mcp-server',
        mcpGitHub: 'github-mcp-server',
        mcpKusto: 'kusto-mcp-server',
        mcpComputerUse: 'computer-use-linux'
      };
      Object.keys(presetStates).forEach(function(id) {
        var checkbox = document.getElementById(id);
        if (checkbox) checkbox.checked = active.indexOf(presetStates[id]) >= 0 || !!saved[presetStates[id]];
      });
    } else {
      statusEl.innerHTML = '<em>Bridge unreachable</em>';
    }
  } catch (e) {
    statusEl.innerHTML = '<em>Bridge not reachable — start <code>tools/acp_bridge.py</code></em>';
  }
}

// Load saved MCP checkbox state
document.addEventListener('DOMContentLoaded', function() {
  try {
    var saved = localStorage.getItem('mcp_config');
    if (saved) {
      populateMCPForm(JSON.parse(saved));
    }
  } catch (e) {}

  // Kusto checkbox toggle: show/hide config fields
  var kustoToggle = document.getElementById('mcpKusto');
  var kustoConfig = document.getElementById('mcpKustoConfig');
  if (kustoToggle && kustoConfig) {
    kustoConfig.style.display = kustoToggle.checked ? 'block' : 'none';
    kustoToggle.addEventListener('change', function() {
      kustoConfig.style.display = kustoToggle.checked ? 'block' : 'none';
      updateKustoSeedButtonState();
    });
  }

  var seedButton = document.getElementById('mcpSeedButton');
  if (seedButton) seedButton.addEventListener('click', seedEvaSchemaFromSettings);
  var ensureTablesButton = document.getElementById('mcpEnsureTablesButton');
  if (ensureTablesButton) ensureTablesButton.addEventListener('click', ensureEvaTablesFromSettings);
  var purgeArtifactsButton = document.getElementById('mcpPurgeArtifactsButton');
  if (purgeArtifactsButton) purgeArtifactsButton.addEventListener('click', purgeArtifactsFromSettings);
  var seedCluster = document.getElementById('mcpKustoCluster');
  var seedDatabase = document.getElementById('mcpKustoDatabase');
  if (seedCluster) seedCluster.addEventListener('input', updateKustoSeedButtonState);
  if (seedDatabase) seedDatabase.addEventListener('input', updateKustoSeedButtonState);
  updateKustoSeedButtonState();

  // Memory backend selector
  initMemoryBackendSelector();
  initProtectedMemoryControls();

  // Re-push saved MCP servers (Kusto cluster/db, Azure, GitHub) to the freshly
  // started bridge so they persist across restarts without manual reconfigure.
  autoApplySavedMCPConfig();
});

// ── Memory backend selector ─────────────────────────────────────────────
function initMemoryBackendSelector() {
  var sel = document.getElementById('memoryBackendSelect');
  var selGeneral = document.getElementById('selMemoryBackend');
  var statusEl = document.getElementById('memoryBackendStatus');
  var statusGeneral = document.getElementById('memoryBackendGeneralStatus');

  // Load persisted preference
  var saved = localStorage.getItem('eva_memory_backend');
  if (saved && (saved === 'kusto' || saved === 'sqlite')) {
    if (sel) sel.value = saved;
    if (selGeneral) selGeneral.value = saved;
  }

  // Fetch current state from bridge
  var bridgeUrl = getACPBridgeUrl();
  fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/backend', {
    signal: AbortSignal.timeout(3000)
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (data.backend) {
      if (sel) sel.value = data.backend;
      if (selGeneral) selGeneral.value = data.backend;
      localStorage.setItem('eva_memory_backend', data.backend);
      var label = data.backend === 'sqlite'
        ? 'Active: local SQLite' + (data.db_path ? ' (' + data.db_path + ')' : '')
        : 'Active: Azure Data Explorer' + (data.cluster ? ' (' + data.database + ')' : '');
      if (statusEl) statusEl.textContent = label;
      if (statusGeneral) statusGeneral.textContent = label;
    }
  }).catch(function() {
    var msg = 'Bridge not reachable — using saved preference';
    if (statusEl) statusEl.textContent = msg;
    if (statusGeneral) statusGeneral.textContent = msg;
  });

  // Change handler for MCP tab selector
  if (sel) sel.addEventListener('change', function() {
    _doSwitchMemoryBackend(sel.value);
  });
  // Change handler for General tab selector is via onchange="switchMemoryBackend()"
}

/**
 * Switch memory backend (called from General tab selector or programmatically).
 */
function switchMemoryBackend(backend) {
  _doSwitchMemoryBackend(backend);
}

function _doSwitchMemoryBackend(backend) {
  var sel = document.getElementById('memoryBackendSelect');
  var selGeneral = document.getElementById('selMemoryBackend');
  var statusEl = document.getElementById('memoryBackendStatus');
  var statusGeneral = document.getElementById('memoryBackendGeneralStatus');

  // Sync both selectors
  if (sel) sel.value = backend;
  if (selGeneral) selGeneral.value = backend;
  localStorage.setItem('eva_memory_backend', backend);

  var msg = 'Switching...';
  if (statusEl) statusEl.textContent = msg;
  if (statusGeneral) statusGeneral.textContent = msg;

  var bridgeUrl = getACPBridgeUrl();
  fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/backend', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ backend: backend }),
    signal: AbortSignal.timeout(5000)
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (data.status === 'ok') {
      var label = backend === 'sqlite'
        ? 'Switched to local SQLite' + (data.db_path ? ' (' + data.db_path + ')' : '')
        : 'Switched to Azure Data Explorer — configure Kusto MCP in Settings > MCP';
      if (statusEl) statusEl.textContent = label;
      if (statusGeneral) statusGeneral.textContent = label;
    } else {
      var err = 'Error: ' + JSON.stringify(data.error || data);
      if (statusEl) statusEl.textContent = err;
      if (statusGeneral) statusGeneral.textContent = err;
    }
  }).catch(function(e) {
    var err = 'Failed to switch: ' + e.message;
    if (statusEl) statusEl.textContent = err;
    if (statusGeneral) statusGeneral.textContent = err;
  });
}

function protectedMemoryBridgeRequest(path, options) {
  var bridgeUrl = getACPBridgeUrl();
  return fetch(bridgeUrl.replace(/\/+$/, '') + path, options || {}).then(function(response) {
    return response.text().then(function(text) {
      var data = {};
      if (text) {
        try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
      }
      if (!response.ok) {
        var message = data && data.error && data.error.message
          ? data.error.message : (data.message || ('HTTP ' + response.status));
        var error = new Error(message);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    });
  });
}

function renderProtectedMemoryStatus(data) {
  var statusEl = document.getElementById('protectedMemoryStatus');
  var recordsEl = document.getElementById('protectedMemoryRecords');
  var setupEl = document.getElementById('protectedMemorySetup');
  var setupTextEl = document.getElementById('protectedMemorySetupText');
  var sessionActionsEl = document.getElementById('protectedMemorySessionActions');
  var storeFieldsEl = document.getElementById('protectedMemoryStoreFields');
  var enrollButton = document.getElementById('protectedMemoryEnrollButton');
  var unlockButton = document.getElementById('protectedMemoryUnlockButton');
  var lockButton = document.getElementById('protectedMemoryLockButton');
  var storeButton = document.getElementById('protectedMemoryStoreButton');
  var storeFileButton = document.getElementById('protectedMemoryStoreFileButton');
  if (!statusEl || !recordsEl) return;
  if (!data) {
    statusEl.textContent = 'Protected memory status unavailable.';
    recordsEl.textContent = '';
    return;
  }
  var enrolled = !!data.enrolled;
  var locked = data.locked !== false;
  var releaseAllowed = !!data.model_release_allowed;
  var state = enrolled ? (locked ? 'Locked' : 'Unlocked') : 'Not configured';
  var provider = data.key_provider_available ? data.key_provider : 'YubiKey provider unavailable';
  var registration = enrolled ? 'YubiKey: registered' : 'YubiKey: not registered';
  var release = locked ? 'model release: off' : (releaseAllowed ? 'model release: approved' : 'model release: off');
  statusEl.textContent = state + ' | ' + registration + ' | ' + release + ' | ' + provider + ' | ' + (data.records || []).length + ' record(s)';
  statusEl.setAttribute('data-status', enrolled ? (locked ? 'warn' : 'info') : 'warn');
  if (setupEl) setupEl.hidden = enrolled;
  if (sessionActionsEl) sessionActionsEl.hidden = !enrolled;
  if (storeFieldsEl) storeFieldsEl.hidden = !enrolled || locked;
  if (enrollButton) enrollButton.disabled = enrolled || !data.key_provider_available;
  if (unlockButton) unlockButton.disabled = !enrolled || !locked;
  if (lockButton) lockButton.disabled = !enrolled || locked;
  if (storeButton) storeButton.disabled = !enrolled || locked;
  if (storeFileButton) storeFileButton.disabled = !enrolled || locked;
  if (setupTextEl && !enrolled) {
    setupTextEl.textContent = data.key_provider_available
      ? 'Connect a touch-enabled YubiKey, then enroll it here. Eva will not store the YubiKey secret.'
      : 'Install the YubiKey manager on this computer, connect a YubiKey, and return here to enroll it.';
  }
  var records = data.records || [];
  if (!records.length) {
    recordsEl.textContent = 'No protected records.';
    return;
  }
  recordsEl.textContent = records.map(function(item) {
    var label = item.PublicLabel || 'protected record';
    var category = item.Category || 'general';
    return label + ' (' + category + ')';
  }).join(' | ');
}

async function refreshProtectedMemoryStatus() {
  var statusEl = document.getElementById('protectedMemoryStatus');
  if (!statusEl) return;
  try {
    var data = await protectedMemoryBridgeRequest('/v1/protected-memory/status');
    renderProtectedMemoryStatus(data);
  } catch (error) {
    statusEl.textContent = error && error.message ? error.message : 'Protected memory status unavailable.';
    statusEl.setAttribute('data-status', 'warn');
  }
}

function protectedMemorySetBusy(button, busy) {
  if (button) button.disabled = !!busy;
}

function _protectedMemoryCaptureIntent(text) {
  var raw = String(text || '').replace(/\s+/g, ' ').trim();
  if (!raw) return null;
  var explicit = raw.match(/^(?:save|store|remember|add)\s+(?:this\s+|that\s+|it\s+)?(?:to|in)\s+protected\s+memory\s*:\s*(.+)$/i);
  if (explicit && explicit[1].trim()) {
    return { value: explicit[1].trim(), label: 'protected memory record', category: 'general' };
  }
  var ssn = raw.match(/\b(?:my\s+)?(?:ssn|social\s+security(?:\s+number)?)\s+(?:is|:)?\s*([0-9]{3}-?[0-9]{2}-?[0-9]{4})\b/i);
  var asksProtected = /\b(?:save|store|remember|add)\b[\s\S]{0,80}\bprotected\s+memory\b|\bprotected\s+memory\b[\s\S]{0,80}\b(?:save|store|remember|add)\b/i.test(raw);
  if (ssn && asksProtected) {
    return { value: ssn[1], label: 'government identifier', category: 'government_identifier' };
  }
  return null;
}

function _appendProtectedMemoryCaptureNotice(text) {
  var output = document.getElementById('txtOutput');
  if (!output) return;
  var bubble = document.createElement('div');
  bubble.className = 'chat-bubble eva-bubble';
  var label = document.createElement('span');
  label.className = 'eva';
  label.textContent = 'Eva:';
  bubble.appendChild(label);
  bubble.appendChild(document.createTextNode(' ' + text));
  output.appendChild(bubble);
  output.scrollTop = output.scrollHeight;
}

async function captureProtectedMemoryFromChat(rawText) {
  var intent = _protectedMemoryCaptureIntent(rawText);
  if (!intent) return false;
  var input = document.getElementById('txtMsg');
  if (input) input.innerHTML = '';
  try {
    await protectedMemoryBridgeRequest('/v1/protected-memory/records', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        value: intent.value,
        public_label: intent.label,
        category: intent.category
      })
    });
    _appendProtectedMemoryCaptureNotice('Stored in protected memory.');
    refreshProtectedMemoryStatus();
  } catch (error) {
    var message = error && error.status === 423
      ? 'Protected memory is locked. Unlock it in Settings before storing this value.'
      : (error && error.message ? error.message : 'Protected memory storage failed.');
    _appendProtectedMemoryCaptureNotice(message);
  }
  return true;
}

function initProtectedMemoryControls() {
  var panel = document.getElementById('protectedMemoryPanel');
  if (!panel || panel.dataset.initialized === '1') return;
  panel.dataset.initialized = '1';
  var enrollButton = document.getElementById('protectedMemoryEnrollButton');
  var unlockButton = document.getElementById('protectedMemoryUnlockButton');
  var lockButton = document.getElementById('protectedMemoryLockButton');
  var refreshButton = document.getElementById('protectedMemoryRefreshButton');
  var storeButton = document.getElementById('protectedMemoryStoreButton');
  var storeFileButton = document.getElementById('protectedMemoryStoreFileButton');
  var valueEl = document.getElementById('protectedMemoryValue');
  var fileEl = document.getElementById('protectedMemoryFile');

  function runAction(button, path, body) {
    protectedMemorySetBusy(button, true);
    return protectedMemoryBridgeRequest(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function() {
      return refreshProtectedMemoryStatus();
    }).catch(function(error) {
      var statusEl = document.getElementById('protectedMemoryStatus');
      if (statusEl) statusEl.textContent = error && error.message ? error.message : 'Protected memory action failed.';
    }).finally(function() {
      protectedMemorySetBusy(button, false);
    });
  }

  if (enrollButton) enrollButton.addEventListener('click', function() {
    runAction(enrollButton, '/v1/protected-memory/enroll', { slot_id: 'yubikey-default' });
  });
  if (unlockButton) unlockButton.addEventListener('click', function() {
    var allowModelRelease = confirm(
      'Allow Eva to use relevant unlocked protected values in this chat session? '
      + 'Those values may be sent to the active AI provider. Locking protected memory revokes this permission.'
    );
    runAction(unlockButton, '/v1/protected-memory/unlock', { allow_model_release: allowModelRelease });
  });
  if (lockButton) lockButton.addEventListener('click', function() {
    protectedMemorySetBusy(lockButton, true);
    protectedMemoryBridgeRequest('/v1/protected-memory/lock', { method: 'POST' })
      .then(function() { return refreshProtectedMemoryStatus(); })
      .catch(function(error) {
        var statusEl = document.getElementById('protectedMemoryStatus');
        if (statusEl) statusEl.textContent = error && error.message ? error.message : 'Protected memory lock failed.';
      }).finally(function() {
        protectedMemorySetBusy(lockButton, false);
      });
  });
  if (refreshButton) refreshButton.addEventListener('click', refreshProtectedMemoryStatus);

  if (storeButton) storeButton.addEventListener('click', function() {
    var value = valueEl ? valueEl.value : '';
    if (!value) return;
    protectedMemorySetBusy(storeButton, true);
    protectedMemoryBridgeRequest('/v1/protected-memory/records', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        value: value,
        public_label: (document.getElementById('protectedMemoryLabel') || {}).value || 'protected memory record',
        category: (document.getElementById('protectedMemoryCategory') || {}).value || 'general'
      })
    }).then(function() {
      return refreshProtectedMemoryStatus();
    }).catch(function(error) {
      var statusEl = document.getElementById('protectedMemoryStatus');
      if (statusEl) statusEl.textContent = error && error.message ? error.message : 'Protected text storage failed.';
    }).finally(function() {
      if (valueEl) valueEl.value = '';
      protectedMemorySetBusy(storeButton, false);
    });
  });

  if (storeFileButton) storeFileButton.addEventListener('click', function() {
    var file = fileEl && fileEl.files ? fileEl.files[0] : null;
    if (!file) return;
    var reader = new FileReader();
    protectedMemorySetBusy(storeFileButton, true);
    reader.onload = function() {
      var encoded = String(reader.result || '').split(',')[1] || '';
      protectedMemoryBridgeRequest('/v1/protected-memory/artifacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content_base64: encoded,
          public_label: (document.getElementById('protectedMemoryLabel') || {}).value || 'protected artifact',
          category: (document.getElementById('protectedMemoryCategory') || {}).value || 'file',
          mime_type: file.type || 'application/octet-stream'
        })
      }).then(function() {
        return refreshProtectedMemoryStatus();
      }).catch(function(error) {
        var statusEl = document.getElementById('protectedMemoryStatus');
        if (statusEl) statusEl.textContent = error && error.message ? error.message : 'Protected file storage failed.';
      }).finally(function() {
        if (fileEl) fileEl.value = '';
        protectedMemorySetBusy(storeFileButton, false);
      });
    };
    reader.onerror = function() {
      if (fileEl) fileEl.value = '';
      protectedMemorySetBusy(storeFileButton, false);
    };
    reader.readAsDataURL(file);
  });

  refreshProtectedMemoryStatus();
}
