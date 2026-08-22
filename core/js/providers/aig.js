// aig.js
// Eva AIG (Artificial Intelligence Gateway) — Intelligent orchestration
// Routes through the bridge which picks the best model for each task,
// maintains Eva's persona, and handles data retrieval seamlessly.

var _aigLmStudioHealth = { baseUrl: '', checkedAt: 0, available: false };

async function waitForPreparedBriefing(bridgeUrl, initialStatus) {
  var latest = initialStatus || {};
  if (latest.status !== 'preparing') return latest;
  var deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    await new Promise(function (resolve) { setTimeout(resolve, 2000); });
    try {
      var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/briefing/status', {
        signal: AbortSignal.timeout(1500)
      });
      if (!response.ok) continue;
      latest = await response.json();
      if (latest.status !== 'preparing') return latest;
      setStatus('info', 'Eva is still preparing the morning briefing...');
    } catch (_) {}
  }
  return latest;
}

function aigLmStudioAvailable() {
  var baseUrl = (typeof getLmStudioBaseUrl === 'function' ? getLmStudioBaseUrl() : '').replace(/\/+$/, '');
  var now = Date.now();
  if (!baseUrl) return false;
  if (_aigLmStudioHealth.baseUrl !== baseUrl || now - _aigLmStudioHealth.checkedAt > 30000) {
    _aigLmStudioHealth.baseUrl = baseUrl;
    _aigLmStudioHealth.checkedAt = now;
    _aigLmStudioHealth.available = false;
    fetch(baseUrl + '/models', { signal: AbortSignal.timeout(1500) }).then(function(response) {
      _aigLmStudioHealth.available = response.ok;
    }).catch(function() {});
  }
  return _aigLmStudioHealth.available;
}

async function aigSend() {
  var txtMsg = document.getElementById('txtMsg');
  var txtOutput = document.getElementById('txtOutput');
  var pendingImageData = window._evaPendingImageData || '';
  var imageMatch = pendingImageData.match(/^data:(image\/(?:jpeg|png|webp|gif));base64,/);
  var imageB64 = imageMatch ? pendingImageData.slice(imageMatch[0].length) : '';
  var imageMime = imageMatch ? imageMatch[1] : 'image/jpeg';

  // Clean HTML artifacts from input
  txtMsg.innerHTML = txtMsg.innerHTML.replace(/<img\b[^>]*>/g, '');

  var sQuestion = txtMsg.innerHTML.replace(/<br>/g, '\n')
    .replace(/<div[^>]*>|<\/div>|&nbsp;|<span[^>]*>|<\/span>/gi, '');
  if (!sQuestion.trim()) {
    alert('Type in your question!');
    txtMsg.focus();
    return;
  }
  window._evaPendingImageData = '';
  var signalContext = (typeof captureSignalDeliveryContext === 'function')
    ? captureSignalDeliveryContext(sQuestion)
    : null;
  var sessionId = (typeof ensureActiveSessionId === 'function')
    ? ensureActiveSessionId() : ((typeof _activeSessionId === 'function') ? (_activeSessionId() || '') : '');
  var turnId = window._evaActiveAuditTurnId || ((typeof EvaRequestRouting !== 'undefined' && EvaRequestRouting.createTurnId) ? EvaRequestRouting.createTurnId() : '');

  // Display user message
  var safeUser = escapeHtml(sQuestion).replace(/\n/g, '<br>');
  txtOutput.innerHTML += '<div class="chat-bubble user-bubble"><span class="user">You:</span> ' + safeUser + '</div>';
  txtMsg.innerHTML = '';
  txtOutput.scrollTop = txtOutput.scrollHeight;

  // Build messages payload
  var storageKey = 'aigMessages';
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
  newMessages.push(imageB64 ? {
    role: 'user',
    content: [
      { type: 'text', text: sQuestion },
      { type: 'image_url', image_url: { url: pendingImageData } }
    ]
  } : { role: 'user', content: sQuestion });

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
  var requestMessages = existingMessages.concat(newMessages);
  var storedMessages = newMessages.map(function (message) {
    if (!Array.isArray(message.content)) return message;
    var text = message.content.filter(function (part) {
      return part && part.type === 'text';
    }).map(function (part) { return part.text || ''; }).join('\n').trim();
    return { role: message.role, content: text || '[Image attachment]' };
  });
  existingMessages = existingMessages.concat(storedMessages);
  localStorage.setItem(storageKey, JSON.stringify(existingMessages));

  var workspaceMcpRequest = /\b(?:mcp|workspace\s+(?:module|server|tool)|work\s*iq)\b/i.test(sQuestion);
  if (workspaceMcpRequest && window.EvaWorkspaces && typeof EvaWorkspaces.mcpContext === 'function') {
    var workspaceMcpContext = EvaWorkspaces.mcpContext();
    if (workspaceMcpContext) requestMessages.push({ role: 'system', content: workspaceMcpContext });
  }

  // Send to AIG orchestrator via bridge
  var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  if (typeof watchACPPermissions === 'function') watchACPPermissions(190000);

  setStatus('info', 'Eva (AIG) processing...');
  // Optional cognitive layer (eva / reviewer).
  // Runs when the Settings toggle is on OR the user message contains an
  // explicit trigger phrase like "trigger the chain" / "use cognition".
  // Falls back to the regular single-shot bridge call on any error.
  var cogDecision = (typeof Cognition !== 'undefined' && Cognition.shouldRun)
                      ? Cognition.shouldRun(sQuestion)
                      : { active: false, reason: null };
  var briefingRequest = /\b(?:morning|daily)\s+(?:briefing|report|update)\b/i.test(sQuestion);
  if (briefingRequest) {
    cogDecision = { active: false, reason: 'briefing-cache' };
    try {
      var briefingResponse = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/briefing/status', {
        signal: AbortSignal.timeout(1500)
      });
      var briefingStatus = briefingResponse.ok ? await briefingResponse.json() : {};
      briefingStatus = await waitForPreparedBriefing(bridgeUrl, briefingStatus);
      if (briefingStatus.status === 'ready' || briefingStatus.status === 'preparing') {
        setStatus('info', briefingStatus.status === 'ready'
          ? 'Eva is using the prepared morning briefing...'
          : 'Eva is preparing the morning briefing...');
      }
    } catch (_) {}
  }
  if (cogDecision.active) {
    if (cogDecision.reason === 'phrase') {
      setStatus('info', 'Eva cognition force-enabled by phrase trigger...');
    }
    var cognitionFinalizing = false;
    try {
      var cogResult = await Cognition.run({
        userMessage: sQuestion,
        messages: requestMessages,
        sessionId: sessionId,
        turnId: turnId,
        imageB64: imageB64,
        imageMime: imageMime,
        forceEnable: cogDecision.reason === 'phrase',
        forcedReason: cogDecision.reason,
        reviewReason: cogDecision.reason
      });
      var cogContent = (cogResult && cogResult.content) ? cogResult.content : '';
      // Execute any [[EVA_ACTION]] blocks Eva emitted, then render.
      var actionsRun = [];
      if (Cognition.executeActions) {
        var execRes = await Cognition.executeActions(cogContent, { userMessage: sQuestion });
        cogContent = execRes.content;
        actionsRun = execRes.actions || [];
      }
      var deferredSignal = false;
      if (Cognition.ensureAgentLaunch) {
        var launchResult = await Cognition.ensureAgentLaunch({
          userMessage: sQuestion,
          content: cogContent,
          actions: actionsRun
        });
        cogContent = launchResult.content;
        actionsRun = launchResult.actions || actionsRun;
        deferredSignal = !!launchResult.deferredSignal;
      }
      cognitionFinalizing = true;
      await renderEvaResponse(cogContent, txtOutput, {
        signalAuthorized: !deferredSignal && !!(signalContext && signalContext.authorized),
        signalMessage: deferredSignal ? '' : (signalContext ? signalContext.message : ''),
        signalRequest: sQuestion,
        nativeRequest: sQuestion,
        turnId: turnId,
        signalContext: signalContext
      });
      if (Cognition.getCfg && Cognition.getCfg().showTrace && Cognition.renderTraceHtml) {
        try {
          txtOutput.innerHTML += Cognition.renderTraceHtml(cogResult.trace || []);
          txtOutput.scrollTop = txtOutput.scrollHeight;
        } catch (_) {}
      }
      if (cogContent) {
        lastResponse = cogContent;
        masterOutput += txtOutput.innerText + '\n';
        localStorage.setItem('masterOutput', masterOutput);
      }
      var cogTag = 'cog:' + (cogResult.evaModel || '?') + '+' +
                   (cogResult.reviewerModel || '?') +
                   '/c' + (cogResult.cycles || 0) +
                   (cogDecision.reason === 'phrase' ? '/forced' : '') +
                   (actionsRun.length ? '/act' + actionsRun.length : '');
      if (cogContent) {
        try {
          fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/reflect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              user_message: sQuestion,
              assistant_message: cogContent,
              model: cogTag,
              session_id: sessionId,
              turn_id: turnId
            }),
            signal: AbortSignal.timeout(5000)
          }).catch(function () {});
        } catch (_) {}
      }
      setStatus('info', 'Eva (AIG, cognition) \u2014 ' +
                (cogResult.evaModel || 'eva') +
                '  [' + cogTag + ']');
      var checkboxC = document.getElementById('autoSpeak');
      if (checkboxC && checkboxC.checked) {
        speakText();
        var audioC = document.getElementById('audioPlayback');
        if (audioC) audioC.setAttribute('autoplay', true);
      }
      return;
    } catch (cogErr) {
      var cogMsg = (cogErr && cogErr.message) ? cogErr.message : String(cogErr);
      if (cognitionFinalizing) {
        setStatus('error', 'Eva could not finalize the response: ' + cogMsg);
        return;
      }
      setStatus('warn', 'Cognition failed, falling back: ' + cogMsg);
      // fall through to single-shot path
    }
  } else {
    // Single-shot path: tell Eva that adaptive review was not selected for
    // this turn, so she neither invents a pipeline nor claims it is disabled.
    var cogState = (typeof Cognition !== 'undefined' && Cognition.getCfg)
                     ? Cognition.getCfg() : null;
    var cogNote = [
      '[Cognition Layer Runtime State - AUTHORITATIVE]',
      'Adaptive review did NOT run for this turn; the selected AIG backend is responding directly.',
      'Adaptive review is controlled by Settings > Models > "Adaptive Review" and activates for',
      'consequential work or an explicit phrase trigger such as "trigger the chain" or "use cognition".',
      'If asked whether review ran, answer truthfully: it did not run for this turn.',
      'Never narrate a fake pipeline (no PHASE 1 / PHASE 2 / PHASE 3 headers, no fabricated reviewer feedback).',
      'The .github/agents/*.agent.md files describe VS Code Copilot review agents and are NOT your runtime tools.',
      'If the user wants the layer, tell them to enable the toggle or use a trigger phrase.'
    ].join('\n');
    if (cogState) {
      cogNote += '\nConfigured models when enabled: eva=' + cogState.evaModel +
                 ', reviewer=' + cogState.reviewerModel +
                 ', maxCycles=' + cogState.maxCycles + '.';
    }
    requestMessages = requestMessages.concat([{ role: 'system', content: cogNote }]);
    existingMessages = existingMessages.concat([{ role: 'system', content: cogNote }]);
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
  }

  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/aig/chat';
    var aigPromptBudget = EvaPromptBudget.compactMessages(requestMessages, {
      budget: 12000,
      recentTurns: 6
    });

    // The selected AIG backend is Eva's primary model. Adaptive review uses a
    // separate reviewer model and must never override this direct responder.
    var aigModel = (document.getElementById('selAIGBackend') || {}).value || 'gpt-5.6-luna';
    var reasoningEffort = (typeof getReasoningEffortForModel === 'function') ? getReasoningEffortForModel('aig') : 'default';

    var provisional = null;
    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: aigPromptBudget.messages,
        prompt_budget: EvaPromptBudget.telemetry(aigPromptBudget),
        user_message: sQuestion,
        session_id: sessionId,
        turn_id: turnId,
        model: aigModel,
        model_policy_mode: (typeof getAIGModelPolicyMode === 'function') ? getAIGModelPolicyMode() : 'auto-balanced',
        max_completion_tokens: (typeof getModelMaxTokens === 'function') ? getModelMaxTokens() : 16384,
        acp_reasoning_effort: reasoningEffort === 'default' ? '' : reasoningEffort,
        lmstudio_base_url: (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : '',
        lmstudio_model: (typeof getLmStudioModel === 'function') ? getLmStudioModel() : '',
        image_b64: imageB64,
        image_mime: imageMime,
        lmstudio_available: aigLmStudioAvailable(),
        openai_api_key: (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : '',
        acp_auto_approve: true,
        stream: true
      })
    });

    if (!resp.ok) {
      var errText = await resp.text();
      var errMsg = 'AIG Error ' + resp.status + ': ' + errText;
      txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">' + escapeHtml(errMsg) + '</span></div>';
      txtOutput.scrollTop = txtOutput.scrollHeight;
      setStatus('error', errMsg);
      return;
    }

    var data = await readEvaStreamingResponse(resp, function (chunk) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      appendEvaStreamingChunk(provisional, chunk, txtOutput);
    });
    removeEvaStreamingBubble(provisional);
    var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
    var modelUsed = data.model || 'aig';

    // Render response
    await renderEvaResponse(content, txtOutput, {
      signalAuthorized: !!(signalContext && signalContext.authorized),
      signalMessage: signalContext ? signalContext.message : '',
      signalRequest: sQuestion,
      nativeRequest: sQuestion,
      turnId: turnId,
      signalContext: signalContext
    });

    if (content) {
      lastResponse = content;
      var outputWithoutTags = txtOutput.innerText + '\n';
      masterOutput += outputWithoutTags;
      localStorage.setItem('masterOutput', masterOutput);
    }

    // Friendly status: pull the actual responder model out of the bridge tag
    // (e.g. "aig:gpt-5.5+copilot-acp" -> responder "gpt-5.5", route "via ACP").
    var responder = modelUsed;
    var routeLabel = '';
    var stripped = String(modelUsed).replace(/^aig:/, '');
    var firstSegment = stripped.split('+')[0] || stripped;
    if (firstSegment) responder = firstSegment;
    var acpTagRe = /(^|\+)(copilot-acp|acp-data|raw-acp|raw-acp-unavailable|acp-default)$/;
    if (/(^|\+)openai-direct($|\+)/.test(stripped)) {
      routeLabel = ' via OpenAI API';
    } else if (/^(claude-|gemini-)/.test(responder) || acpTagRe.test(stripped) || responder === 'acp-default') {
      routeLabel = ' via ACP';
    } else if (/^(gpt-|o\d|deepseek-|llama-)/.test(responder)) {
      routeLabel = ' via AIG';
    }
    if (responder === 'unavailable' || responder === 'raw-acp-unavailable') {
      setStatus('error', 'Eva (AIG) responder unavailable (' + modelUsed + ')');
    } else if (typeof reportCompletionTruncation === 'function' && reportCompletionTruncation(data)) {
    } else {
      setStatus('info', 'Eva (AIG) \u2014 ' + responder + routeLabel + '  [' + modelUsed + ']');
    }

    // Auto-speak
    var checkbox = document.getElementById('autoSpeak');
    if (checkbox && checkbox.checked) {
      speakText();
      var audio = document.getElementById('audioPlayback');
      if (audio) audio.setAttribute('autoplay', true);
    }

  } catch (err) {
    removeEvaStreamingBubble(typeof provisional === 'undefined' ? null : provisional);
    var errorMessage = err.message || String(err);
    if (errorMessage.includes('Failed to fetch') || errorMessage.includes('NetworkError')) {
      errorMessage += ' — Is the ACP bridge server running? Start it with: python3 tools/acp_bridge.py --enable-kusto-mcp';
    }
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">AIG Error:</span> ' + escapeHtml(errorMessage) + '</div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
    setStatus('error', errorMessage);
  }
}
