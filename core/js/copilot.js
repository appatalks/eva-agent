// copilot.js
// GitHub Copilot integration — two modes:
//   1. GitHub Models API (direct REST, requires PAT)
//   2. ACP Bridge (local server bridging Copilot CLI's Agent Client Protocol)
//
// Mode is determined by the selected model:
//   copilot-*     → GitHub Models API
//   copilot-acp   → ACP Bridge (uses copilot CLI via tools/acp_bridge.py)

// --- Helpers ---

// Track last user message for post-response reflection (cognition layer)
var _copilotLastUserMsg = '';

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

function _copilotRequestIsCurrent(capturedEnvelope) {
  return !!(capturedEnvelope && typeof isCurrentRequestEnvelope === 'function' &&
    isCurrentRequestEnvelope(capturedEnvelope));
}

async function copilotSend(capturedEnvelope) {
  capturedEnvelope = capturedEnvelope || ((typeof captureRequestEnvelope === 'function')
    ? captureRequestEnvelope() : null);
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
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
    await _copilotSendACP(existingMessages, sQuestion, txtOutput, storageKey, capturedEnvelope);
  } else {
    await _copilotSendModelsAPI(existingMessages, selModel.value, txtOutput, storageKey, sQuestion, capturedEnvelope);
  }
}

// --- GitHub Models API mode ---

async function _copilotSendModelsAPI(messages, modelValue, txtOutput, storageKey, question, capturedEnvelope) {
  var githubToken = getAuthKey('GITHUB_PAT');
  var model = modelValue.replace(/^copilot-/, '');

  // --- Cognition: Fetch memory context from bridge and inject into system message ---
  var lastUserMsg = '';
  for (var i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') { lastUserMsg = messages[i].content || ''; break; }
  }
  try {
    var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
    var ctxResp = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/context?message=' + encodeURIComponent(lastUserMsg), {
      signal: AbortSignal.timeout(3000)
    });
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
    if (ctxResp.ok) {
      var ctxData = await ctxResp.json();
      if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
      if (ctxData.context && ctxData.cognition_enabled) {
        // Prepend memory context to the first system message, or insert one
        var injected = false;
        for (var j = 0; j < messages.length; j++) {
          if (messages[j].role === 'system' || messages[j].role === 'developer') {
            messages[j].content = ctxData.context + '\n\n' + messages[j].content;
            injected = true;
            break;
          }
        }
        if (!injected) {
          messages.unshift({ role: 'system', content: ctxData.context });
        }
      }
    }
  } catch (e) {
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
    // Bridge not available — continue without memory
  }
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;

  var temp = (typeof getModelTemperature === 'function') ? getModelTemperature() : 0.7;
  var maxTok = (typeof getModelMaxTokens === 'function') ? getModelMaxTokens() : 4096;

  // Map short model names to GitHub Models API publisher/model format
  // See: https://github.com/marketplace/models/catalog
  var _modelMap = {
    'gpt-4o': 'openai/gpt-4o',
    'gpt-4o-mini': 'openai/gpt-4o-mini',
    'gpt-4.1': 'openai/gpt-4.1',
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
    messages: messages,
    temperature: temp,
    max_tokens: maxTok
  };

  // Reasoning models: add reasoning_effort, remove temperature
  var reasoningModels = ['o3-mini', 'o4-mini', 'deepseek-r1'];
  if (reasoningModels.indexOf(model) >= 0) {
    var re = (typeof getReasoningEffort === 'function') ? getReasoningEffort() : 'medium';
    payload.reasoning_effort = re;
    delete payload.temperature;
  }

  // GPT-5 family: use max_completion_tokens, remove temperature and stop
  if (model === 'gpt-5') {
    delete payload.temperature;
  }

  setStatus('info', 'Sending to GitHub Models API (' + model + ')...');

  try {
    var url = 'https://models.github.ai/inference/chat/completions';

    var resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + githubToken,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;

    if (!resp.ok) {
      await _copilotHandleHTTPError(resp, txtOutput, capturedEnvelope);
      return;
    }

    var data = await resp.json();
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
    await _copilotRenderResponse(data, txtOutput, model, false, question, capturedEnvelope);

  } catch (err) {
    _copilotHandleFetchError(err, txtOutput, capturedEnvelope);
  }
}

// --- ACP Bridge mode ---

async function _copilotSendACP(messages, question, txtOutput, storageKey, capturedEnvelope) {
  // Auto-detect bridge URL (tries configured, same-host, localhost)
  var bridgeUrl = await detectACPBridge();
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;

  // Get selected ACP model (empty string = use CLI default)
  var acpModel = (typeof getACPModel === 'function') ? getACPModel() : '';
  var modelLabel = acpModel ? 'Copilot ACP (' + acpModel + ')' : 'Copilot ACP (default)';

  setStatus('info', 'Sending to ' + modelLabel + ' via ' + bridgeUrl + '...');

  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/chat/completions';

    var payload = { messages: messages, model: 'copilot-acp' };
    if (capturedEnvelope) Object.assign(payload, capturedEnvelope);
    if (acpModel) payload.acp_model = acpModel;

    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;

    if (!resp.ok) {
      await _copilotHandleHTTPError(resp, txtOutput, capturedEnvelope);
      return;
    }

    var data = await resp.json();
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
    await _copilotRenderResponse(data, txtOutput, modelLabel, false, question, capturedEnvelope);

  } catch (err) {
    _copilotHandleFetchError(
      { message: 'Copilot request failed.' }, txtOutput, capturedEnvelope
    );
  }
}

// --- Shared response rendering ---

async function _copilotRenderResponse(data, txtOutput, modelLabel, bridgeOwnedReflection, question, capturedEnvelope) {
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
  var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
  var canonicalResponse = typeof canonicalizeEvaResponse === 'function'
    ? canonicalizeEvaResponse(content, {
        allowCamera: typeof _isExplicitCameraRequest === 'function' &&
          _isExplicitCameraRequest(question)
      }) : { text: String(content || '') };
  content = canonicalResponse.text;

  if (!bridgeOwnedReflection && content && question &&
      typeof finalizeDirectProviderTurn === 'function') {
    await finalizeDirectProviderTurn(
      question, content, modelLabel, capturedEnvelope
    );
    if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
  }

  // Use unified renderer
  if (!await renderEvaResponse(
    content, txtOutput, capturedEnvelope, [], canonicalResponse
  )) return;
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;

  if (content) {
    lastResponse = content;
    var outputWithoutTags = txtOutput.innerText + '\n';
    masterOutput += outputWithoutTags;
    localStorage.setItem('masterOutput', masterOutput);
  }

  setStatus('info', 'Response received from ' + modelLabel);

  // Auto-speak
  var checkbox = document.getElementById('autoSpeak');
  if (checkbox && checkbox.checked) {
    speakText();
    var audio = document.getElementById('audioPlayback');
    if (audio) audio.setAttribute('autoplay', true);
  }
}

// --- Error handling ---

async function _copilotHandleHTTPError(resp, txtOutput, capturedEnvelope) {
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
  var errMsg = 'Copilot request failed (HTTP ' + resp.status + ').';
  txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">' + escapeHtml(errMsg) + '</span></div>';
  txtOutput.scrollTop = txtOutput.scrollHeight;
  setStatus('error', errMsg);
}

function _copilotHandleFetchError(err, txtOutput, capturedEnvelope) {
  if (!_copilotRequestIsCurrent(capturedEnvelope)) return;
  console.error('Copilot request failed');
  var errorMessage = 'Copilot request failed.';
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
}

function sanitizeMCPConfig(cfg) {
  if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) return {};
  function exactArray(actual, expected) {
    return Array.isArray(actual) && actual.length === expected.length &&
      actual.every(function(value, index) { return value === expected[index]; });
  }
  function safeEnv(raw, allowed) {
    if (raw === undefined) return {};
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    var output = {};
    var keys = Object.keys(raw);
    for (var i = 0; i < keys.length; i += 1) {
      var key = keys[i];
      var value = raw[key];
      if (allowed.indexOf(key) === -1) return null;
      if (key === '_useGitHubPAT') {
        if (value !== true) return null;
      } else if (typeof value !== 'string' || value.indexOf('\0') !== -1 ||
                 value.length > 16384) return null;
      output[key] = value;
    }
    return output;
  }
  function exactServer(value) {
    return value && typeof value === 'object' && !Array.isArray(value) &&
      Object.keys(value).every(function(key) {
        return key === 'command' || key === 'args' || key === 'env';
      }) && typeof value.command === 'string' && Array.isArray(value.args) &&
      value.args.every(function(arg) { return typeof arg === 'string'; });
  }
  function pythonScript(value, basename) {
    if (!exactServer(value)) return false;
    var command = value.command.split(/[\\/]/).pop();
    var script = value.args.length === 1
      ? value.args[0].replace(/\\/g, '/') : '';
    return /^python3(?:\.\d+)?$/.test(command) &&
      (script === 'tools/' + basename || script.endsWith('/tools/' + basename));
  }
  var clean = {};
  Object.keys(cfg).forEach(function(name) {
    var value = cfg[name];
    if (!exactServer(value)) return;
    if (name === 'azure-mcp-server' && value.command === 'npx' &&
        exactArray(value.args, ['-y', '@azure/mcp@latest', 'server', 'start'])) {
      var azureEnv = safeEnv(value.env, ['AZURE_MCP_COLLECT_TELEMETRY']);
      if (azureEnv !== null &&
          (azureEnv.AZURE_MCP_COLLECT_TELEMETRY || 'false') === 'false') {
        clean[name] = value;
      }
      return;
    }
    if (name === 'github-mcp-server' && value.command === 'docker' &&
        exactArray(value.args, [
          'run', '-i', '--rm', '-e', 'GITHUB_PERSONAL_ACCESS_TOKEN',
          'ghcr.io/github/github-mcp-server'
        ]) && safeEnv(value.env, [
          '_useGitHubPAT', 'GITHUB_PERSONAL_ACCESS_TOKEN'
        ]) !== null) {
      clean[name] = value;
      return;
    }
    if (name === 'kusto-mcp-server' && pythonScript(value, 'kusto_mcp.py') &&
        safeEnv(value.env, [
          'KUSTO_ACCESS_TOKEN', 'KUSTO_CLUSTER_URL', 'KUSTO_DATABASE',
          'KUSTO_DATABASE_LOCKED'
        ]) !== null) {
      clean[name] = value;
      return;
    }
    if ((name === 'sqlite' || name === 'sqlite-mcp-server' ||
         name === 'eva-sqlite') && pythonScript(value, 'sqlite_mcp.py') &&
        safeEnv(value.env, ['EVA_MEMORY_DB']) !== null) clean[name] = value;
  });
  return clean;
}

// Re-apply the saved MCP config to a freshly started bridge.
// The bridge is a new process on every launch with no MCP servers configured,
// so without this the user would have to re-Configure Kusto/Azure/GitHub MCP
// after each restart. Reads the persisted config directly from localStorage so
// it does not depend on the Settings form fields being populated yet.
async function autoApplySavedMCPConfig() {
  try {
    var repairBridge = await detectACPBridge();
    var healthResp = await fetch(repairBridge.replace(/\/+$/, '') + '/health');
    if (healthResp.ok) {
      var health = await healthResp.json();
      if (health && health.repair_required === true) {
        setStatus('warn', 'Runtime repair required: choose Local or Cloud mode explicitly.');
        return;
      }
    }
  } catch (_) {
    // Existing bounded restore retries handle a bridge that is not ready yet.
  }
  var saved;
  try {
    saved = JSON.parse(localStorage.getItem('mcp_config') || 'null');
  } catch (e) {
    saved = null;
  }
  saved = sanitizeMCPConfig(saved);
  localStorage.setItem('mcp_config', JSON.stringify(saved));

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
          saved = sanitizeMCPConfig(restored);
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
          github_pat: (typeof getAuthKey === 'function') ? getAuthKey('GITHUB_PAT') : ''
        })
      });
      if (resp.ok) {
        var data = await resp.json();
        setStatus('info', 'MCP restored: ' + ((data.active_servers || []).join(', ') || 'none'));
        if (typeof refreshMCPStatus === 'function') refreshMCPStatus();
        return;
      }
    } catch (e) {
      // Bridge not ready yet; wait briefly and retry.
    }
    await new Promise(function (r) { setTimeout(r, 1500); });
  }
}

async function applyMCPConfig() {
  var bridgeUrl = await detectACPBridge();
  var mcpServers = {};

  // Azure MCP
  var azureCheck = document.getElementById('mcpAzure');
  if (azureCheck && azureCheck.checked) {
    mcpServers['azure-mcp-server'] = {
      command: 'npx',
      args: ['-y', '@azure/mcp@latest', 'server', 'start'],
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
      setStatus('info', 'MCP configured: ' + (data.active_servers || []).join(', '));
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
    if (typeof purgeAssets !== 'function') throw new Error('Artifact purge is unavailable');
    var result = await purgeAssets({ skipConfirm: true });
    if (!result || result.ok !== true) throw new Error('Artifact purge failed');
    var data = result.data || {};
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
      if (active.length > 0) {
        statusEl.innerHTML = '<strong>Active MCP Servers:</strong> ' + active.map(function(s) { return '<span class="mcp-badge">' + escapeHtml(s) + '</span>'; }).join(' ');
        // Sync checkboxes
        var azureCheck = document.getElementById('mcpAzure');
        var githubCheck = document.getElementById('mcpGitHub');
      if (azureCheck) azureCheck.checked = active.indexOf('azure-mcp-server') >= 0;
      if (githubCheck) githubCheck.checked = active.indexOf('github-mcp-server') >= 0;
        // Kusto
        var kustoCheckS = document.getElementById('mcpKusto');
        if (kustoCheckS) kustoCheckS.checked = active.indexOf('kusto-mcp-server') >= 0;
      } else {
        statusEl.innerHTML = '<em>No MCP servers active</em>';
      }
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
      if (data.reconciliation && !data.reconciliation.reconciled) {
        var warning = ' Warning: ' + data.reconciliation.message;
        if (statusEl) statusEl.textContent += warning;
        if (statusGeneral) statusGeneral.textContent += warning;
      }
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

  var previous = localStorage.getItem('eva_memory_backend') || 'sqlite';
  // Sync both selectors visually while request is pending
  if (sel) sel.value = backend;
  if (selGeneral) selGeneral.value = backend;

  var msg = 'Switching...';
  if (statusEl) statusEl.textContent = msg;
  if (statusGeneral) statusGeneral.textContent = msg;

  var bridgeUrl = getACPBridgeUrl();
  var payload = { backend: backend };
  var mutationEnvelope = (typeof captureMutationEnvelope === 'function')
    ? captureMutationEnvelope()
    : ((typeof captureOperationEnvelope === 'function') ? captureOperationEnvelope() : null);
  if (mutationEnvelope) Object.assign(payload, mutationEnvelope);
  function sendSwitch(confirmOverride) {
    payload.confirm_unreconciled = !!confirmOverride;
    return fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/backend', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(5000)
    }).then(function(r) {
      return r.json().then(function(data) { return { response: r, data: data }; });
    });
  }
  sendSwitch(false).then(function(result) {
    if (result.response.status === 409 && result.data.requires_confirmation) {
      var detail = (result.data.reconciliation && result.data.reconciliation.message) || 'Memory journals are not reconciled.';
      if (!window.confirm(detail + '\n\nSwitch anyway and record an audit event?')) {
        throw new Error('Switch cancelled: ' + detail);
      }
      return sendSwitch(true);
    }
    return result;
  }).then(function(result) {
    var data = result.data;
    if (data.status === 'ok') {
      localStorage.setItem('eva_memory_backend', backend);
      var label = backend === 'sqlite'
        ? 'Switched to local SQLite' + (data.db_path ? ' (' + data.db_path + ')' : '')
        : 'Switched to Azure Data Explorer — configure Kusto MCP in Settings > MCP';
      if (statusEl) statusEl.textContent = label;
      if (statusGeneral) statusGeneral.textContent = label;
    } else {
      var err = 'Error: ' + JSON.stringify(data.error || data);
      if (statusEl) statusEl.textContent = err;
      if (statusGeneral) statusGeneral.textContent = err;
      if (sel) sel.value = previous;
      if (selGeneral) selGeneral.value = previous;
    }
  }).catch(function(e) {
    var err = 'Failed to switch: ' + e.message;
    if (statusEl) statusEl.textContent = err;
    if (statusGeneral) statusGeneral.textContent = err;
    if (sel) sel.value = previous;
    if (selGeneral) selGeneral.value = previous;
  });
}
