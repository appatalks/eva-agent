// voice.js — Wake-word voice activation for Eva
// Listens continuously for "Eva" wake word, then captures the command and sends it.

var _voiceRecognition = null;
var _voiceListening = false;
var _voiceAwake = false; // true after hearing "Eva", waiting for command

/** Start continuous voice listening */
function startVoiceListener() {
  var SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    console.warn('[Voice] SpeechRecognition not supported in this browser');
    _setMicStatus('unsupported');
    return;
  }

  if (_voiceListening) {
    stopVoiceListener();
    return;
  }

  _voiceRecognition = new SpeechRec();
  var voiceLanguage = typeof getLocalVoicesLanguage === 'function' ? getLocalVoicesLanguage() : 'auto';
  // Browser SpeechRecognition has no portable automatic language mode. Local
  // Whisper handles automatic detection; browser recognition defaults to English.
  _voiceRecognition.lang = voiceLanguage === 'ko' ? 'ko-KR' : 'en-US';
  _voiceRecognition.continuous = true;
  _voiceRecognition.interimResults = false;

  _voiceRecognition.onstart = function() {
    _voiceListening = true;
    _setMicStatus('listening');
  };

  _voiceRecognition.onresult = function(event) {
    // Process only new results
    for (var i = event.resultIndex; i < event.results.length; i++) {
      if (!event.results[i].isFinal) continue;
      var transcript = event.results[i][0].transcript.trim();
      _handleVoiceTranscript(transcript);
    }
  };

  _voiceRecognition.onerror = function(event) {
    // 'aborted' is normal when listening continuously; retain no-speech as a diagnostic.
    if (event.error === 'aborted') return;
    if (event.error === 'no-speech') {
      if (typeof EvaLearning !== 'undefined' && EvaLearning) EvaLearning.recordVoiceDiagnostic({ type: 'error', reason: 'no-speech' });
      return;
    }
    if (typeof EvaLearning !== 'undefined' && EvaLearning) EvaLearning.recordVoiceDiagnostic({ type: event.error === 'not-allowed' ? 'denied' : 'error', reason: event.error });
    console.warn('[Voice] Error:', event.error);
    if (event.error === 'not-allowed') {
      _setMicStatus('denied');
      _voiceListening = false;
      return;
    }
  };

  _voiceRecognition.onend = function() {
    // Auto-restart if we were listening (browser stops after silence)
    if (_voiceListening) {
      try { _voiceRecognition.start(); }
      catch(e) {
        // Small delay before retry (browser may need a moment)
        setTimeout(function() {
          if (_voiceListening) {
            try { _voiceRecognition.start(); } catch(e2) {
              _voiceListening = false;
              _setMicStatus('off');
            }
          }
        }, 300);
      }
    }
  };

  _voiceRecognition.start();
}

/** Stop continuous voice listening */
function stopVoiceListener() {
  _voiceListening = false;
  _voiceAwake = false;
  if (_voiceRecognition) {
    try { _voiceRecognition.stop(); } catch(e) {}
    _voiceRecognition = null;
  }
  _setMicStatus('off');
}

/** Process a transcript chunk */
function _handleVoiceTranscript(transcript) {
  if (typeof evaTextPromptConsumeVoice === 'function' && evaTextPromptConsumeVoice(transcript)) {
    _voiceAwake = false;
    _setMicStatus('listening');
    return;
  }
  var lower = transcript.toLowerCase();

  // Check for wake word "eva" anywhere in the phrase
  var evaIdx = lower.indexOf('eva');

  if (evaIdx >= 0) {
    // Extract the command part after "eva"
    var command = transcript.substring(evaIdx + 3).trim();

    // Remove leading punctuation/filler
    command = command.replace(/^[,.\s]+/, '').trim();

    if (command.length > 1) {
      // Got wake word + command in one phrase — send immediately
      _sendVoiceCommand(command);
    } else {
      // Just "Eva" — enter awake mode, wait for next phrase
      _voiceAwake = true;
      _setMicStatus('awake');
      // Auto-timeout after 10 seconds of silence
      if (_voiceAwakeTimer) clearTimeout(_voiceAwakeTimer);
      _voiceAwakeTimer = setTimeout(function() {
        _voiceAwake = false;
        _setMicStatus('listening');
      }, 10000);
    }
    return;
  }

  if (_voiceAwake) {
    // We heard "Eva" previously — this phrase is the command
    _voiceAwake = false;
    if (_voiceAwakeTimer) clearTimeout(_voiceAwakeTimer);
    if (transcript.length > 1) {
      _sendVoiceCommand(transcript);
    } else {
      _setMicStatus('listening');
    }
    return;
  }

  // No wake word and not awake — ignore (Eva stays quiet)
}

var _voiceAwakeTimer = null;

/** Send a voice command to the chat */
function _sendVoiceCommand(command) {
  if (typeof _vvStopTTS === 'function') _vvStopTTS();
  else if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (_) {} }
  _setMicStatus('sending');
  var voiceTurnId = typeof evaCreateAuditTurnId === 'function' ? evaCreateAuditTurnId() : '';
  if (typeof evaAuditEvent === 'function') {
    evaAuditEvent('voice.command', 'submitted', {
      correlation_id: voiceTurnId,
      request_chars: String(command || '').length
    });
  }

  if (_runVoiceNavigationCommand(command, voiceTurnId)) {
    setTimeout(function() { _setMicStatus('listening'); }, 500);
    return;
  }

  var txtMsg = document.getElementById('txtMsg');
  if (txtMsg) {
    txtMsg.textContent = command;
  }
  window._evaPendingAuditTurnId = voiceTurnId;

  // Use sendData (routes to the selected model)
  if (typeof sendData === 'function') {
    sendData();
  }

  // Return to listening after a short delay
  setTimeout(function() {
    _setMicStatus('listening');
  }, 1000);
}

function _runVoiceNavigationCommand(command, turnId) {
  if (!turnId && typeof evaCreateAuditTurnId === 'function') turnId = evaCreateAuditTurnId();
  var phrase = String(command || '').trim().toLowerCase();
  if (!phrase) return false;
  if (/^(?:new|start) (?:a )?(?:chat|conversation)$/.test(phrase)) {
    var newChatResult = window.EvaHarness ? EvaHarness.execute({ action: 'new_chat' }) : null;
    Promise.resolve(newChatResult).then(function(result) {
      if (typeof evaAuditEvent === 'function') evaAuditEvent('native_action', result && result.ok ? 'completed' : 'failed', { correlation_id: turnId || '', action: 'new_chat' });
    }).catch(function() {
      if (typeof evaAuditEvent === 'function') evaAuditEvent('native_action', 'failed', { correlation_id: turnId || '', action: 'new_chat' });
    });
    return true;
  }
  if (!window.EvaHarness || typeof EvaHarness.resolveNavigationRequest !== 'function') return false;
  var route = EvaHarness.resolveNavigationRequest(phrase, { directUser: true });
  if (!route) return false;
  if (route.action === 'consider_terminal_task') return false;
  if (typeof evaAuditEvent === 'function') evaAuditEvent('direct_route', 'started', { correlation_id: turnId || '', action: route.action || 'navigate', label: route.target || '' });
  if (typeof evaTextPromptCancel === 'function') evaTextPromptCancel();
  var pendingResult = route.action && route.action !== 'navigate'
    ? EvaHarness.execute(route)
    : EvaHarness.navigate(route.target);
  Promise.resolve(pendingResult).then(function(navigationResult) {
    if (!navigationResult.ok) {
      if (typeof setStatus === 'function') setStatus('error', navigationResult.message || 'Voice navigation failed.');
      if (typeof evaAuditEvent === 'function') evaAuditEvent('native_action', typeof evaAuditOutcome === 'function' ? evaAuditOutcome(navigationResult.data && navigationResult.data.outcome, false) : 'failed', { correlation_id: turnId || '', action: route.action || 'navigate', label: route.target || '', reason: navigationResult.data && navigationResult.data.reason || 'failed' });
      return;
    }
    var reply = route.action === 'plan_terminal_task'
      ? navigationResult.message
      : route.action === 'type_terminal_command'
      ? 'I typed that command in the terminal for your review.'
      : route.action === 'run_terminal_command'
      ? 'I submitted that command to the terminal.'
      : route.action === 'list_github_repositories'
      ? 'I listed your GitHub repositories in Workspaces. Choose one there to import.'
      : route.action === 'continue_github_repositories'
      ? navigationResult.message
      : route.action === 'authorize_github'
      ? 'I started GitHub device authorization in Workspaces.'
      : route.action && route.action !== 'navigate'
      ? navigationResult.message
      : route.target === 'workspaces'
      ? 'I opened Workspaces. I can review imported projects and their files, Git status and diffs, active coding runs, generated assets, and workspace-scoped MCP configuration. I can inspect read-only workspace commands autonomously. Changes, deletions, external actions, and unclassified commands still require your approval.'
      : 'Opening ' + route.label + '.';
    if (typeof setStatus === 'function') setStatus('info', navigationResult.message || 'Opened ' + route.label + '.');
    if (typeof evaAuditEvent === 'function') evaAuditEvent('native_action', typeof evaAuditOutcome === 'function' ? evaAuditOutcome(navigationResult.data && navigationResult.data.outcome, true) : 'completed', { correlation_id: turnId || '', action: route.action || 'navigate', label: route.target || '' });
    if (typeof recordConversationTurn === 'function') recordConversationTurn(command, reply);
    if (typeof speakText === 'function') speakText(reply);
  }).catch(function(error) {
    if (typeof setStatus === 'function') setStatus('error', error && error.message ? error.message : 'Voice navigation failed.');
    if (typeof evaAuditEvent === 'function') evaAuditEvent('native_action', 'failed', { correlation_id: turnId || '', action: route.action || 'navigate', label: route.target || '' });
  });
  return true;
}

/** Update mic button visual state */
function _setMicStatus(status) {
  var buttons = [document.getElementById('micButton'), document.getElementById('evaSidebarMicButton')].filter(Boolean);
  var compact = document.querySelector('.eva-sidebar-voice');
  var compactStatus = document.getElementById('evaSidebarVoiceStatus');
  if (compact) compact.dataset.state = status;
  if (compactStatus) compactStatus.textContent = status === 'off' ? 'VOICE' : status.toUpperCase();

  // Remove all states
  buttons.forEach(function(btn) { btn.classList.remove('pulsate', 'mic-listening', 'mic-awake', 'mic-sending', 'mic-denied'); });

  switch (status) {
    case 'listening':
      buttons.forEach(function(btn) { btn.classList.add('mic-listening'); btn.title = 'Listening for "Eva"... (click to stop)'; });
      break;
    case 'awake':
      buttons.forEach(function(btn) { btn.classList.add('mic-awake', 'pulsate'); btn.title = 'Eva is listening — speak your command'; });
      break;
    case 'sending':
      buttons.forEach(function(btn) { btn.classList.add('mic-sending'); btn.title = 'Processing...'; });
      break;
    case 'denied':
      buttons.forEach(function(btn) { btn.classList.add('mic-denied'); btn.title = 'Microphone access denied'; });
      break;
    case 'unsupported':
      buttons.forEach(function(btn) { btn.title = 'Speech recognition not supported'; });
      break;
    default:
      buttons.forEach(function(btn) { btn.title = 'Click to start voice listener'; });
      break;
  }
}

/** Toggle voice listener — replaces the old startSpeechRecognition */
function startSpeechRecognition() {
  if (_voiceListening) {
    stopVoiceListener();
  } else {
    startVoiceListener();
  }
}
