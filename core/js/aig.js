// aig.js
// Eva AIG (Artificial Intelligence Gateway) — Intelligent orchestration
// Routes through the bridge which picks the best model for each task,
// maintains Eva's persona, and handles data retrieval seamlessly.

function _aigClosedActionReceipts(actions) {
  if (!Array.isArray(actions)) return [];
  return actions.slice(0, 16).reduce(function(receipts, entry) {
    if (!entry || (entry.id !== 'file.download' && entry.id !== 'file.open')) {
      return receipts;
    }
    var result = entry.result;
    if (entry.ok !== true || !result || typeof result !== 'object') {
      receipts.push({ id: entry.id, state: 'failed' });
      return receipts;
    }
    var artifact = {
      filename: String(result.filename || ''),
      mime: String(result.mime || ''),
      session_id: String(result.session_id || ''),
      artifact_id: String(result.artifact_id || ''),
      digest: String(result.digest || ''),
      generation: String(result.generation || ''),
      size: result.size
    };
    if (/^[A-Za-z0-9._-]{1,128}$/.test(artifact.filename) &&
        artifact.mime.length <= 128 &&
        /^[A-Za-z0-9!#$&^_.+\-]+\/[A-Za-z0-9!#$&^_.+\-]+$/.test(artifact.mime) &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(artifact.session_id) &&
        /^[0-9a-f]{32}$/.test(artifact.artifact_id) &&
        /^[0-9a-f]{64}$/.test(artifact.digest) &&
        /^[1-9][0-9]{0,39}$/.test(artifact.generation) &&
        Number.isInteger(artifact.size) && artifact.size >= 0 &&
        artifact.size <= 16 * 1024 * 1024) {
      receipts.push({ id: entry.id, state: 'succeeded', artifact: artifact });
    } else {
      receipts.push({ id: entry.id, state: 'failed' });
    }
    return receipts;
  }, []);
}

async function aigSend(forcedModel, capturedEnvelope) {
  capturedEnvelope = capturedEnvelope || ((typeof captureRequestEnvelope === 'function')
    ? captureRequestEnvelope() : null);
  function requestIsCurrent() {
    return !!(capturedEnvelope && typeof isCurrentRequestEnvelope === 'function' &&
      isCurrentRequestEnvelope(capturedEnvelope));
  }
  if (!requestIsCurrent()) return;
  var txtMsg = document.getElementById('txtMsg');
  var txtOutput = document.getElementById('txtOutput');
  var assistantPersisted = false;

  function persistCanonicalAssistant(storageKey, content) {
    if (assistantPersisted || !content) return;
    var messages;
    try { messages = JSON.parse(localStorage.getItem(storageKey) || '[]'); }
    catch (_) { messages = []; }
    if (!Array.isArray(messages)) messages = [];
    messages.push({ role: 'assistant', content: String(content) });
    localStorage.setItem(storageKey, JSON.stringify(messages));
    assistantPersisted = true;
  }

  // Clean HTML artifacts from input
  txtMsg.innerHTML = txtMsg.innerHTML.replace(/<img\b[^>]*>/g, '');

  var sQuestion = txtMsg.innerHTML.replace(/<br>/g, '\n')
    .replace(/<div[^>]*>|<\/div>|&nbsp;|<span[^>]*>|<\/span>/gi, '');
  if (!sQuestion.trim()) {
    alert('Type in your question!');
    txtMsg.focus();
    return;
  }

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
  var trustedArtifacts = (typeof getTrustedArtifacts === 'function')
    ? getTrustedArtifacts().map(function(row) {
        return {
          filename: row.filename, mime: row.mime, size: row.size,
          session_id: row.session_id,
          artifact_id: row.artifact_id, digest: row.digest,
          generation: row.generation
        };
      }) : [];

  // Send to AIG orchestrator via bridge
  var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';

  var _envelope = capturedEnvelope || ((typeof captureRequestEnvelope === 'function')
    ? captureRequestEnvelope()
    : { session_id: window._evaSessionId || '', turn_id: window._evaTurnId || '' });
  var _turnId = _envelope.turn_id;
  window._evaSessionId = _envelope.session_id;
  window._evaTurnId = _turnId;

  setStatus('info', 'Eva (AIG) processing...');
  if (typeof _copilotLastUserMsg !== 'undefined') { _copilotLastUserMsg = sQuestion; }

  // Optional cognitive layer (eva / reviewer).
  // Runs when the Settings toggle is on OR the user message contains an
  // explicit trigger phrase like "trigger the chain" / "use cognition".
  // Falls back to the regular single-shot bridge call on any error.
  var cogDecision = (typeof Cognition !== 'undefined' && Cognition.shouldRun)
                      ? Cognition.shouldRun(sQuestion)
                      : { active: false, reason: null };
  if (cogDecision.active) {
    if (cogDecision.reason === 'phrase') {
      setStatus('info', 'Eva cognition force-enabled by phrase trigger...');
    }
    try {
      var cogResult = await Cognition.run({
        userMessage: sQuestion,
        messages: existingMessages,
        envelope: _envelope,
        trustedArtifacts: trustedArtifacts,
        forceEnable: cogDecision.reason === 'phrase',
        forcedReason: cogDecision.reason
      });
      if (!requestIsCurrent()) return;
      var cogContent = (cogResult && cogResult.content) ? cogResult.content : '';
      // Execute any [[EVA_ACTION]] blocks Eva emitted, then render.
      var actionsRun = [];
      if (Cognition.executeActions) {
        var execRes = await Cognition.executeActions(cogContent, _envelope);
        if (!requestIsCurrent()) return;
        cogContent = execRes.content;
        actionsRun = execRes.actions || [];
      }
      var cogCanonical = canonicalizeEvaResponse(cogContent, {
        allowCamera: typeof _isExplicitCameraRequest === 'function' &&
          _isExplicitCameraRequest(sQuestion) && actionsRun.length === 0,
        allowAgentControls: actionsRun.length === 0
      });
      cogContent = cogCanonical.text;
      var cogTag = 'cog:' + (cogResult.evaModel || '?') + '+' +
                   (cogResult.reviewerModel || '?') +
                   '/c' + (cogResult.cycles || 0) +
                   (cogDecision.reason === 'phrase' ? '/forced' : '') +
                   (actionsRun.length ? '/act' + actionsRun.length : '');
      var cognitionReceipts = _aigClosedActionReceipts(actionsRun);
      if (cogContent || cognitionReceipts.length) {
        if (typeof finalizeDirectProviderTurn === 'function') {
          try {
            await finalizeDirectProviderTurn(
              sQuestion, cogContent, cogTag, _envelope, cognitionReceipts
            );
          } catch (finalizationError) {
            finalizationError.code = 'EVA_DURABLE_FINALIZATION_FAILED';
            throw finalizationError;
          }
          if (!requestIsCurrent()) return;
        }
      }
      persistCanonicalAssistant(storageKey, cogContent);
      if (!await renderEvaResponse(
        cogContent, txtOutput, _envelope, actionsRun, cogCanonical
      )) return;
      if (!requestIsCurrent()) return;
      if (cogContent) {
        lastResponse = cogContent;
        masterOutput += cogContent + '\n';
        localStorage.setItem('masterOutput', masterOutput);
      }
      if (Cognition.getCfg && Cognition.getCfg().showTrace && Cognition.renderTraceHtml) {
        try {
          txtOutput.innerHTML += Cognition.renderTraceHtml(cogResult.trace || []);
          txtOutput.scrollTop = txtOutput.scrollHeight;
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
      if (!requestIsCurrent()) return;
      var cogMsg = 'Cognition request failed.';
      if (cogErr && cogErr.code === 'EVA_DURABLE_FINALIZATION_FAILED') {
        cogMsg = 'Durable cognition finalization failed.';
        txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">AIG Error:</span> ' + escapeHtml(cogMsg) + '</div>';
        setStatus('error', 'Durable cognition finalization failed.');
        return;
      }
      setStatus('warn', 'Cognition failed, falling back: ' + cogMsg);
      // fall through to single-shot path
    }
  } else {
    // Single-shot path: tell Eva the truth about her own cognitive layer so
    // she does not hallucinate a fake pipeline run when asked about it.
    var cogState = (typeof Cognition !== 'undefined' && Cognition.getCfg)
                     ? Cognition.getCfg() : null;
    var cogNote = [
      '[Cognition Layer Runtime State - AUTHORITATIVE]',
      'The cognitive layer (eva / reviewer) is currently DISABLED for this turn.',
      'It is controlled by the user via Settings > Models > "Enable Cognitive Layer",',
      'or by an explicit phrase trigger such as "trigger the chain" or "use cognition".',
      'You are NOT running inside that layer right now. You are the single-shot AIG responder.',
      'If asked whether the layer ran, answer truthfully: it did not.',
      'Never narrate a fake pipeline (no PHASE 1 / PHASE 2 / PHASE 3 headers, no fabricated reviewer feedback).',
      'The .github/agents/*.agent.md files describe VS Code Copilot review agents and are NOT your runtime tools.',
      'If the user wants the layer, tell them to enable the toggle or use a trigger phrase.'
    ].join('\n');
    if (cogState) {
      cogNote += '\nConfigured models when enabled: eva=' + cogState.evaModel +
                 ', reviewer=' + cogState.reviewerModel +
                 ', maxCycles=' + cogState.maxCycles + '.';
    }
    existingMessages = existingMessages.concat([{ role: 'system', content: cogNote }]);
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
  }

  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/aig/chat';

    // Prefer cognition evaModel when cognition is configured,
    // otherwise fall back to the AIG backend selector dropdown.
    var aigModel = forcedModel || (document.getElementById('selAIGBackend') || {}).value || 'claude-opus-4.8';
    if (!forcedModel && typeof Cognition !== 'undefined' && Cognition.getCfg) {
      var cogModelCfg = Cognition.getCfg();
      if (cogModelCfg.enabled && cogModelCfg.evaModel) {
        aigModel = cogModelCfg.evaModel;
      }
    }

    var requestMessages = existingMessages.slice();
    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: requestMessages,
        user_message: sQuestion,
        model: aigModel,
        session_id: _envelope.session_id,
        turn_id: _envelope.turn_id,
        request_id: _envelope.request_id,
        correlation_id: _envelope.correlation_id,
        lmstudio_base_url: (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : '',
        lmstudio_model: (typeof getLmStudioModel === 'function') ? getLmStudioModel() : '',
        github_pat: (typeof getAuthKey === 'function') ? getAuthKey('GITHUB_PAT') : '',
        openai_api_key: (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : '',
        trusted_artifacts: trustedArtifacts
      })
    });
    if (!requestIsCurrent()) return;

    if (!resp.ok) {
      if (!requestIsCurrent()) return;
      var errMsg = 'AIG request failed (HTTP ' + resp.status + ').';
      txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">' + escapeHtml(errMsg) + '</span></div>';
      txtOutput.scrollTop = txtOutput.scrollHeight;
      setStatus('error', errMsg);
      return;
    }

    var data = await resp.json();
    if (!requestIsCurrent()) return;
    var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
    var modelUsed = data.model || 'aig';
    var normalActions = [];
    if (typeof Cognition !== 'undefined' && Cognition.executeActions) {
      var normalExecution = await Cognition.executeActions(content, _envelope);
      if (!requestIsCurrent()) return;
      content = normalExecution.content;
      normalActions = normalExecution.actions || [];
    }
    var normalCanonical = canonicalizeEvaResponse(content, {
      allowCamera: typeof _isExplicitCameraRequest === 'function' &&
        _isExplicitCameraRequest(sQuestion) && normalActions.length === 0,
      allowAgentControls: normalActions.length === 0
    });
    content = normalCanonical.text;
    if (typeof finalizeDirectProviderTurn === 'function') {
      await finalizeDirectProviderTurn(
        sQuestion, content, modelUsed, _envelope,
        _aigClosedActionReceipts(normalActions)
      );
      if (!requestIsCurrent()) return;
    }
    persistCanonicalAssistant(storageKey, content);

    // Render response
    if (!await renderEvaResponse(
      content, txtOutput, _envelope, normalActions, normalCanonical
    )) return;
    if (!requestIsCurrent()) return;

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
    if (/^(claude-|gemini-)/.test(responder) || acpTagRe.test(stripped) || responder === 'acp-default') {
      routeLabel = ' via ACP';
    } else if (/^(gpt-|o\d|deepseek-|llama-)/.test(responder)) {
      routeLabel = ' via GitHub Models';
    }
    if (responder === 'unavailable' || responder === 'raw-acp-unavailable') {
      setStatus('error', 'Eva (AIG) responder unavailable (' + modelUsed + ')');
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
    if (!requestIsCurrent()) return;
    var errorMessage = err && err.code === 'EVA_DURABLE_FINALIZATION_FAILED'
      ? 'Durable AIG finalization failed.' : 'AIG request failed.';
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">AIG Error:</span> ' + escapeHtml(errorMessage) + '</div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
    setStatus('error', errorMessage);
  }
}
