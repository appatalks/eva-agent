// aig.js
// Eva AIG (Artificial Intelligence Gateway) — Intelligent orchestration
// Routes through the bridge which picks the best model for each task,
// maintains Eva's persona, and handles data retrieval seamlessly.

var _aigLmStudioHealth = { baseUrl: '', checkedAt: 0, available: false };

function isExplicitLocationMemoryRequest(text) {
  var value = String(text || '');
  return /\b(?:save|remember|store|note)\b[\s\S]{0,100}\b(?:durable\s+)?memory\b/i.test(value) &&
    /\b(?:i\s+(?:live|am\s+(?:in|based|located))|i['’]?m\s+(?:in|based|located)|my\s+location\s+is)\b/i.test(value);
}

async function saveExplicitLocationMemory(bridgeUrl, userMessage, sessionId, turnId) {
  var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/remember-location', {
    method: 'POST',
    headers: (typeof getBridgeCapabilityHeaders === 'function') ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_message: userMessage, session_id: sessionId, turn_id: turnId }),
    signal: AbortSignal.timeout(5000)
  });
  if (!response.ok) {
    var body = await response.json().catch(function () { return {}; });
    throw new Error((body.error && body.error.message) || ('HTTP ' + response.status));
  }
  return response.json();
}

function briefingItems(summary, limit) {
  var items = [];
  var current = null;
  String(summary || '').split('\n').forEach(function (line) {
    var heading = line.match(/^\s*-\s+(.+?)\s*$/);
    if (heading) {
      if (current) items.push(current);
      current = { title: heading[1], detail: '' };
      return;
    }
    if (!current || !line.trim() || /^\s*https?:\/\//i.test(line)) return;
    if (!current.detail) current.detail = line.trim();
  });
  if (current) items.push(current);
  return items.slice(0, limit || 5);
}

function formatBriefingSection(summary, limit) {
  var items = briefingItems(summary, limit);
  if (!items.length) return String(summary || '').trim();
  return items.map(function (item) {
    return '- **' + item.title + '**' + (item.detail ? '\n  ' + item.detail : '');
  }).join('\n');
}

function requestedStockSymbol(text) {
  var value = String(text || '');
  var dollar = value.match(/\$([A-Za-z]{1,10})\b/);
  if (dollar) return dollar[1].toUpperCase();
  var afterSubject = value.match(/\b(?:stock|ticker|symbol)\s+(?:price\s+)?(?:of\s+|for\s+)?([A-Za-z]{1,10})\b/i);
  if (afterSubject) return afterSubject[1].toUpperCase();
  var beforeSubject = value.match(/\b([A-Z]{1,10})\b[^.!?]{0,80}\b(?:stock|share|ticker|quote|price)\b/);
  if (beforeSubject) return beforeSubject[1].toUpperCase();
  return '';
}

function formatBriefingQuote(quote, requestedSymbol) {
  quote = quote || {};
  var symbol = String(quote.symbol || requestedSymbol || 'Requested ticker').toUpperCase();
  if (!quote || typeof quote.price !== 'number') {
    return '### Requested quote\n\n_Verified current price for ' + symbol + ' is unavailable from the configured local quote source._';
  }
  var currency = String(quote.currency || '').trim();
  var exchange = String(quote.exchange || '').trim();
  var lines = ['### Requested quote', '- **' + symbol + (exchange ? ' · ' + exchange : '') + '**: ' + (currency ? currency + ' ' : '') + quote.price];
  if (typeof quote.change === 'number' && typeof quote.change_percent === 'number') {
    var sign = quote.change > 0 ? '+' : '';
    lines.push('- Session move: ' + sign + quote.change + ' (' + sign + quote.change_percent + '%)');
    lines.push('- Analysis: The verified receipt shows a ' + (quote.change > 0 ? 'move above' : quote.change < 0 ? 'move below' : 'move in line with') + ' the previous close. It does not include enough historical or fundamental data for a broader assessment.');
  } else {
    lines.push('- Analysis: The verified receipt does not include a prior-close comparison or broader historical data.');
  }
  return lines.join('\n');
}

async function fetchBriefingQuote(bridgeUrl, userMessage, sessionId) {
  var symbol = requestedStockSymbol(userMessage);
  if (!symbol) return { content: '', available: false };
  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/data/retrieve?message=' + encodeURIComponent(userMessage) + '&session_id=' + encodeURIComponent(sessionId || '');
    var response = await fetch(url, {
      headers: (typeof getBridgeCapabilityHeaders === 'function') ? getBridgeCapabilityHeaders() : {},
      signal: AbortSignal.timeout(25000)
    });
    var body = await response.json().catch(function () { return {}; });
    var receipt = {};
    try { receipt = JSON.parse(String(body.data || '')); } catch (_) {}
    var quote = receipt.stock_quote || {};
    return { content: formatBriefingQuote(quote, symbol), available: typeof quote.price === 'number' };
  } catch (_) {
    return { content: formatBriefingQuote({}, symbol), available: false };
  }
}

function formatPreparedBriefing(status, preparing, requestedQuote) {
  status = status || {};
  var sources = status.sources || {};
  var lines = ['## Morning briefing'];
  var rendered = 0;
  var weather = sources.weather || {};
  var weatherSummary = String(weather.summary || '').trim();
  if (weather.status === 'ready' && weatherSummary) {
    lines.push('### Weather', formatBriefingSection(weatherSummary, 1));
    rendered += 1;
  } else if (!preparing && weatherSummary) {
    lines.push('### Weather', '_' + weatherSummary + '_');
    rendered += 1;
  }

  var mail = sources.mail || {};
  var memory = sources.memory || {};
  var dayContext = [mail, memory].map(function (source) {
    return source.status === 'ready' || source.status === 'partial' ? String(source.summary || '').trim() : '';
  }).filter(Boolean).join('\n');
  if (dayContext) {
    lines.push('### Your day', dayContext);
    rendered += 1;
  }

  var news = sources.news || {};
  if (news.status === 'ready' && news.summary) {
    lines.push('### Headlines', formatBriefingSection(news.summary, 5));
    rendered += 1;
  } else if (!preparing && news.summary) {
    lines.push('### Headlines', '_Unavailable: ' + String(news.summary).trim() + '_');
    rendered += 1;
  }

  var markets = sources.markets || {};
  if (markets.status === 'ready' && markets.summary) {
    lines.push('### Markets', formatBriefingSection(markets.summary, 3));
    rendered += 1;
  } else if (!preparing && markets.summary) {
    lines.push('### Markets', '_Unavailable: ' + String(markets.summary).trim() + '_');
    rendered += 1;
  }
  if (requestedQuote) {
    lines.push(requestedQuote);
    rendered += 1;
  }
  if (preparing) lines.push('_Gathering the remaining live sections..._');
  if (!rendered && !preparing) lines.push('Live briefing data is unavailable right now.');
  return lines.join('\n\n');
}

function updatePreparedBriefingPreview(preview, status, txtOutput) {
  if (!preview || !preview.body) return;
  preview.body.innerHTML = renderMarkdown(formatPreparedBriefing(status, true));
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

async function waitForPreparedBriefing(bridgeUrl, initialStatus, onProgress) {
  var latest = initialStatus || {};
  if (latest.status !== 'preparing') return latest;
  var deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    await new Promise(function (resolve) { setTimeout(resolve, 750); });
    try {
      var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/briefing/status', {
        signal: AbortSignal.timeout(1200)
      });
      if (!response.ok) continue;
      latest = await response.json();
      if (typeof onProgress === 'function') onProgress(latest);
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

  if (isExplicitLocationMemoryRequest(sQuestion)) {
    try {
      await saveExplicitLocationMemory(bridgeUrl, sQuestion, sessionId, turnId);
      var savedLocationReply = "I've saved your location to my durable memory for future briefings.";
      await renderEvaResponse(savedLocationReply, txtOutput, {
        nativeRequest: sQuestion,
        turnId: turnId
      });
      existingMessages.push({ role: 'assistant', content: savedLocationReply });
      localStorage.setItem(storageKey, JSON.stringify(existingMessages));
      lastResponse = savedLocationReply;
      masterOutput += txtOutput.innerText + '\n';
      localStorage.setItem('masterOutput', masterOutput);
      if (typeof evaAuditEvent === 'function') {
        evaAuditEvent('native_action', 'completed', {
          correlation_id: turnId,
          action: 'remember_location',
          label: 'Durable Memory'
        });
      }
      setStatus('info', 'Eva saved your location to durable memory.');
      return;
    } catch (memoryError) {
      setStatus('warn', 'Eva could not save the location directly; continuing with the normal response.');
    }
  }

  setStatus('info', 'Eva (AIG) processing...');
  // Optional cognitive layer (eva / reviewer).
  // Runs when the Settings toggle is on OR the user message contains an
  // explicit trigger phrase like "trigger the chain" / "use cognition".
  // Falls back to the regular single-shot bridge call on any error.
  var cogDecision = (typeof Cognition !== 'undefined' && Cognition.shouldRun)
                      ? Cognition.shouldRun(sQuestion)
                      : { active: false, reason: null };
  var requestedQuoteSymbol = requestedStockSymbol(sQuestion);
  var isDirectQuoteRequest = !!requestedQuoteSymbol
    && /\b(?:stock|share|ticker|quote|price)\b/i.test(sQuestion)
    && !/\b(?:analy[sz]e|forecast|strategy|compare|valuation|fundamental|thesis)\b/i.test(sQuestion);
  if (isDirectQuoteRequest && cogDecision.reason !== 'phrase') {
    cogDecision = { active: false, reason: 'direct-quote' };
  }
  var briefingRequest = /\b(?:morning|daily)\s+(?:briefing|report|update)\b/i.test(sQuestion);
  if (briefingRequest) {
    cogDecision = { active: false, reason: 'briefing-cache' };
    var briefingPreview = null;
    try {
      var briefingResponse = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/briefing/refresh', {
        method: 'POST',
        headers: (typeof getBridgeCapabilityHeaders === 'function') ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
        signal: AbortSignal.timeout(1500)
      });
      if (!briefingResponse.ok) throw new Error('Briefing status unavailable');
      var briefingStatus = briefingResponse.ok ? await briefingResponse.json() : {};
      if (briefingStatus.status === 'preparing') {
        briefingPreview = document.createElement('div');
        briefingPreview.className = 'chat-bubble eva-bubble eva-briefing-preview';
        briefingPreview.innerHTML = '<span class="eva">Eva:</span> <div class="md"></div>';
        txtOutput.appendChild(briefingPreview);
        updatePreparedBriefingPreview({ body: briefingPreview.querySelector('.md') }, briefingStatus, txtOutput);
        setStatus('info', 'Eva is preparing the morning briefing...');
      }
      briefingStatus = await waitForPreparedBriefing(bridgeUrl, briefingStatus, function (latest) {
        if (briefingPreview) {
          updatePreparedBriefingPreview({ body: briefingPreview.querySelector('.md') }, latest, txtOutput);
        }
      });
      if (briefingPreview && briefingPreview.parentNode) briefingPreview.parentNode.removeChild(briefingPreview);
      var requestedQuote = await fetchBriefingQuote(bridgeUrl, sQuestion, sessionId);
      var briefingContent = formatPreparedBriefing(briefingStatus, briefingStatus.status === 'preparing', requestedQuote.content);
      await renderEvaResponse(briefingContent, txtOutput, {
        nativeRequest: sQuestion,
        turnId: turnId
      });
      existingMessages.push({ role: 'assistant', content: briefingContent });
      localStorage.setItem(storageKey, JSON.stringify(existingMessages));
      lastResponse = briefingContent;
      masterOutput += txtOutput.innerText + '\n';
      localStorage.setItem('masterOutput', masterOutput);
      setStatus('info', 'Eva morning briefing - prepared live sources');
      var briefingAutoSpeak = document.getElementById('autoSpeak');
      if (briefingAutoSpeak && briefingAutoSpeak.checked) speakText();
      return;
    } catch (_) {}
  }
  if (isDirectQuoteRequest) {
    var quotePreview = createEvaStreamingBubble(txtOutput);
    updateEvaStreamingStatus(quotePreview, {
      phase: 'thinking',
      text: 'Eva is retrieving live data...'
    }, txtOutput);
    var directQuote = await fetchBriefingQuote(bridgeUrl, sQuestion, sessionId);
    removeEvaStreamingBubble(quotePreview);
    await renderEvaResponse(directQuote.content, txtOutput, {
      nativeRequest: sQuestion,
      turnId: turnId
    });
    existingMessages.push({ role: 'assistant', content: directQuote.content });
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
    lastResponse = directQuote.content;
    masterOutput += txtOutput.innerText + '\n';
    localStorage.setItem('masterOutput', masterOutput);
    setStatus(directQuote.available ? 'info' : 'warn', directQuote.available
      ? 'Eva verified the current ' + requestedQuoteSymbol + ' quote.'
      : 'Eva could not verify the current ' + requestedQuoteSymbol + ' quote.');
    var directQuoteAutoSpeak = document.getElementById('autoSpeak');
    if (directQuoteAutoSpeak && directQuoteAutoSpeak.checked) speakText();
    return;
  }
  var provisional = createEvaStreamingBubble(txtOutput);
  updateEvaStreamingStatus(provisional, {
    phase: 'thinking',
    text: 'Eva is preparing context...'
  }, txtOutput);
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
        reviewReason: cogDecision.reason,
        onStatus: function (text) {
          if (!provisional) return;
          updateEvaStreamingStatus(provisional, {
            phase: 'thinking',
            text: text
          }, txtOutput);
        }
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
      removeEvaStreamingBubble(provisional);
      provisional = null;
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

    if (!provisional) {
      provisional = createEvaStreamingBubble(txtOutput);
      updateEvaStreamingStatus(provisional, {
        phase: 'thinking',
        text: 'Eva is preparing context...'
      }, txtOutput);
    }
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
      removeEvaStreamingBubble(provisional);
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
    }, function (event) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      updateEvaStreamingStatus(provisional, event, txtOutput);
    }, function (reasoning) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      appendEvaStreamingReasoning(provisional, reasoning, txtOutput);
    });
    removeEvaStreamingBubble(provisional);
    var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
    var responseMessage = (data.choices && data.choices[0] && data.choices[0].message) || {};
    var reasoningContent = responseMessage.reasoning_content || '';
    var modelUsed = data.model || 'aig';

    // Render response
    await renderEvaResponse(content, txtOutput, {
      signalAuthorized: !!(signalContext && signalContext.authorized),
      signalMessage: signalContext ? signalContext.message : '',
      signalRequest: sQuestion,
      nativeRequest: sQuestion,
      turnId: turnId,
      signalContext: signalContext,
      reasoningContent: reasoningContent
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
