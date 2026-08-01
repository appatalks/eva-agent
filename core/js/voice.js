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
    // 'no-speech' and 'aborted' are normal when listening continuously
    if (event.error === 'no-speech' || event.error === 'aborted') return;
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
  _setMicStatus('sending');

  var txtMsg = document.getElementById('txtMsg');
  if (txtMsg) {
    txtMsg.textContent = command;
  }

  // Use sendData (routes to the selected model)
  if (typeof sendData === 'function') {
    sendData();
  }

  // Return to listening after a short delay
  setTimeout(function() {
    _setMicStatus('listening');
  }, 1000);
}

/** Update mic button visual state */
function _setMicStatus(status) {
  var btn = document.getElementById('micButton');
  if (!btn) return;

  // Remove all states
  btn.classList.remove('pulsate', 'mic-listening', 'mic-awake', 'mic-sending', 'mic-denied');

  switch (status) {
    case 'listening':
      btn.classList.add('mic-listening');
      btn.title = 'Listening for "Eva"... (click to stop)';
      break;
    case 'awake':
      btn.classList.add('mic-awake', 'pulsate');
      btn.title = 'Eva is listening — speak your command';
      break;
    case 'sending':
      btn.classList.add('mic-sending');
      btn.title = 'Processing...';
      break;
    case 'denied':
      btn.classList.add('mic-denied');
      btn.title = 'Microphone access denied';
      break;
    case 'unsupported':
      btn.title = 'Speech recognition not supported';
      break;
    default:
      btn.title = 'Click to start voice listener';
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
