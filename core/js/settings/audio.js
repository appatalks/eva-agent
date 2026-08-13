function getLiveTranslationTarget() {
  var language = (localStorage.getItem('live_translation_target') || 'en').trim().toLowerCase();
  return ['en', 'ko', 'es', 'uk'].indexOf(language) >= 0 ? language : 'en';
}

function getLiveTranslationModel() {
  var model = (localStorage.getItem('live_translation_model') || 'aig').trim();
  return ['openai:gpt-4.1-nano', 'aig', 'lmstudio'].indexOf(model) >= 0 ? model : 'aig';
}

function getPreferredAudioInputDeviceId() {
  try { return localStorage.getItem('audio_input_device_id') || ''; } catch (e) { return ''; }
}

function getPreferredAudioOutputDeviceId() {
  try { return localStorage.getItem('audio_output_device_id') || ''; } catch (e) { return ''; }
}

function _audioDeviceStatus(message) {
  var status = document.getElementById('audioDeviceStatus');
  if (status) status.textContent = message || '';
}

function _addAudioDeviceOptions(select, devices, selectedId, fallbackName) {
  if (!select) return;
  select.textContent = '';
  var defaultOption = document.createElement('option');
  defaultOption.value = '';
  defaultOption.textContent = 'System default';
  select.appendChild(defaultOption);
  devices.forEach(function(device, index) {
    var option = document.createElement('option');
    option.value = device.deviceId;
    option.textContent = device.label || fallbackName + ' ' + (index + 1);
    select.appendChild(option);
  });
  select.value = Array.from(select.options).some(function(option) { return option.value === selectedId; }) ? selectedId : '';
}

function refreshAudioDevicePreferences(requestPermission) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    _audioDeviceStatus('Audio device selection is unavailable in this browser.');
    return Promise.resolve();
  }
  var permission = requestPermission && navigator.mediaDevices.getUserMedia
    ? navigator.mediaDevices.getUserMedia({ audio: true }).then(function(stream) {
      stream.getTracks().forEach(function(track) { track.stop(); });
    })
    : Promise.resolve();
  return permission.then(function() {
    return navigator.mediaDevices.enumerateDevices();
  }).then(function(devices) {
    var input = document.getElementById('audioInputDevice');
    var output = document.getElementById('audioOutputDevice');
    _addAudioDeviceOptions(input, devices.filter(function(device) { return device.kind === 'audioinput'; }), getPreferredAudioInputDeviceId(), 'Microphone');
    _addAudioDeviceOptions(output, devices.filter(function(device) { return device.kind === 'audiooutput'; }), getPreferredAudioOutputDeviceId(), 'Speakers');
    _audioDeviceStatus('');
  }).catch(function(error) {
    _audioDeviceStatus('Unable to list audio devices: ' + (error && error.message ? error.message : 'permission was denied.'));
  });
}

function applyPreferredAudioOutputDevice() {
  var deviceId = getPreferredAudioOutputDeviceId();
  var audio = document.getElementById('audioPlayback');
  var tasks = [];
  if (audio && typeof audio.setSinkId === 'function') tasks.push(audio.setSinkId(deviceId));
  if (_vv.audioCtx && typeof _vv.audioCtx.setSinkId === 'function') tasks.push(_vv.audioCtx.setSinkId(deviceId));
  if (!tasks.length) {
    if (deviceId) _audioDeviceStatus('This browser uses the system default speaker.');
    return Promise.resolve();
  }
  return Promise.all(tasks).then(function() {
    _audioDeviceStatus('');
  }).catch(function(error) {
    _audioDeviceStatus('Unable to use the selected speakers: ' + (error && error.message ? error.message : 'device unavailable.'));
  });
}

function getPreferredMicrophoneConstraints() {
  var inputId = getPreferredAudioInputDeviceId();
  var audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (inputId) audio.deviceId = { exact: inputId };
  return { audio: audio };
}

function initAudioDevicePreferences() {
  var input = document.getElementById('audioInputDevice');
  var output = document.getElementById('audioOutputDevice');
  var refresh = document.getElementById('refreshAudioDevicesButton');
  if (input && !input._audioDeviceBound) {
    input._audioDeviceBound = true;
    input.addEventListener('change', function() {
      localStorage.setItem('audio_input_device_id', input.value);
      if (_vvIsActive() && (_vv.recognition || _vv.whisperMode)) {
        _vvStopListening();
        _vvStartListening();
      }
    });
  }
  if (output && !output._audioDeviceBound) {
    output._audioDeviceBound = true;
    output.addEventListener('change', function() {
      localStorage.setItem('audio_output_device_id', output.value);
      applyPreferredAudioOutputDevice();
    });
  }
  if (refresh && !refresh._audioDeviceBound) {
    refresh._audioDeviceBound = true;
    refresh.addEventListener('click', function() { refreshAudioDevicePreferences(true); });
  }
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener && !navigator.mediaDevices._evaAudioDeviceBound) {
    navigator.mediaDevices._evaAudioDeviceBound = true;
    navigator.mediaDevices.addEventListener('devicechange', function() { refreshAudioDevicePreferences(false); });
  }
  refreshAudioDevicePreferences(false);
  applyPreferredAudioOutputDevice();
}

function initAudioPreferences() {
  var engine = document.getElementById('selEngine');
  var voice = document.getElementById('selVoice');
  var autoSpeak = document.getElementById('autoSpeak');
  var endpointDelay = document.getElementById('voiceEndpointDelay');
  var liveTranslationTarget = document.getElementById('liveTranslationTarget');
  var liveTranslationModel = document.getElementById('liveTranslationModel');

  if (engine) {
    var savedEngine = localStorage.getItem('tts_engine');
    if (savedEngine && Array.from(engine.options).some(function(option) { return option.value === savedEngine; })) {
      engine.value = savedEngine;
    }
    engine.addEventListener('change', function() {
      localStorage.setItem('tts_engine', engine.value);
    });
  }

  if (voice) {
    var savedVoice = localStorage.getItem('tts_voice');
    if (savedVoice && Array.from(voice.options).some(function(option) { return option.value === savedVoice; })) {
      voice.value = savedVoice;
    }
    voice.addEventListener('change', function() {
      localStorage.setItem('tts_voice', voice.value);
    });
  }

  if (autoSpeak) {
    autoSpeak.checked = localStorage.getItem('tts_auto_speak') === '1';
    autoSpeak.addEventListener('change', function() {
      localStorage.setItem('tts_auto_speak', autoSpeak.checked ? '1' : '0');
    });
  }

  if (endpointDelay) {
    var savedEndpointDelay = parseInt(localStorage.getItem('voice_endpoint_delay_ms'), 10);
    if (Array.from(endpointDelay.options).some(function(option) { return parseInt(option.value, 10) === savedEndpointDelay; })) {
      endpointDelay.value = String(savedEndpointDelay);
    }
    endpointDelay.addEventListener('change', function() {
      _vv.endpointDelayMs = parseInt(endpointDelay.value, 10) || 2200;
      localStorage.setItem('voice_endpoint_delay_ms', String(_vv.endpointDelayMs));
      if (_vv.endpoint) _vv.endpoint.setDelay(_vv.endpointDelayMs);
    });
  }

  if (liveTranslationTarget) {
    liveTranslationTarget.value = getLiveTranslationTarget();
    liveTranslationTarget.addEventListener('change', function() {
      localStorage.setItem('live_translation_target', liveTranslationTarget.value);
      _vvSyncLiveTranslationControls();
    });
  }

  if (liveTranslationModel) {
    liveTranslationModel.value = getLiveTranslationModel();
    liveTranslationModel.addEventListener('change', function() {
      localStorage.setItem('live_translation_model', liveTranslationModel.value);
    });
  }

  initAudioDevicePreferences();
}