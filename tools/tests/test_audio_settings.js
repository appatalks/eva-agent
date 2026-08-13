#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function makeSelect() {
  let selectedValue = '';
  const select = {
    options: [],
    listeners: {},
    appendChild(option) {
      this.options.push(option);
    },
    addEventListener(type, listener) {
      this.listeners[type] = listener;
    },
    dispatch(type) {
      if (this.listeners[type]) this.listeners[type]();
    }
  };
  Object.defineProperty(select, 'textContent', {
    get() { return ''; },
    set() { this.options.length = 0; selectedValue = ''; }
  });
  Object.defineProperty(select, 'value', {
    get() { return selectedValue; },
    set(value) {
      selectedValue = this.options.some((option) => option.value === value) ? value : '';
    }
  });
  return select;
}

function makeCheckbox() {
  return {
    checked: false,
    listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; }
  };
}

function makeConfiguredSelect(values) {
  const select = makeSelect();
  values.forEach((value) => select.appendChild({ value, textContent: value }));
  return select;
}

function makeContext() {
  const storage = new Map();
  const fields = {
    audioInputDevice: makeSelect(),
    audioOutputDevice: makeSelect(),
    refreshAudioDevicesButton: { listeners: {}, addEventListener(type, listener) { this.listeners[type] = listener; } },
    audioDeviceStatus: { textContent: '' },
    audioPlayback: {
      sinkIds: [],
      setSinkId(id) {
        this.sinkIds.push(id);
        return Promise.resolve();
      }
    },
    selEngine: makeConfiguredSelect(['browser', 'openai', 'local-voices']),
    selVoice: makeConfiguredSelect(['alloy', 'nova']),
    autoSpeak: makeCheckbox(),
    voiceEndpointDelay: makeConfiguredSelect(['1500', '2200', '3000']),
    liveTranslationTarget: makeConfiguredSelect(['en', 'ko', 'es', 'uk']),
    liveTranslationModel: makeConfiguredSelect(['openai:gpt-4.1-nano', 'aig', 'lmstudio'])
  };
  const mediaDevices = {
    devices: [
      { kind: 'audioinput', deviceId: 'mic-1', label: 'Desk microphone' },
      { kind: 'audioinput', deviceId: 'mic-2', label: '' },
      { kind: 'audiooutput', deviceId: 'speaker-1', label: 'Desk speakers' },
      { kind: 'videoinput', deviceId: 'camera-1', label: 'Camera' }
    ],
    listeners: {},
    enumerateDevices() { return Promise.resolve(this.devices); },
    getUserMedia() {
      return Promise.resolve({ getTracks() { return [{ stop() {} }]; } });
    },
    addEventListener(type, listener) { this.listeners[type] = listener; }
  };
  const context = {
    Array,
    JSON,
    Map,
    Promise,
    String,
    document: {
      createElement() { return {}; },
      getElementById(id) { return fields[id] || null; }
    },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
      removeItem(key) { storage.delete(key); }
    },
    navigator: { mediaDevices },
    _vv: {
      audioCtx: {
        sinkIds: [],
        setSinkId(id) {
          this.sinkIds.push(id);
          return Promise.resolve();
        }
      },
      recognition: { active: true },
      whisperMode: false,
      endpointDelayMs: 2200,
      endpoint: {
        delays: [],
        setDelay(value) { this.delays.push(value); }
      }
    },
    _vvIsActive() { return true; },
    _vvStopListening() { context.stopListeningCalls += 1; },
    _vvStartListening() { context.startListeningCalls += 1; },
    _vvSyncLiveTranslationControls() { context.translationSyncCalls += 1; },
    translationSyncCalls: 0,
    stopListeningCalls: 0,
    startListeningCalls: 0
  };
  context.window = context;
  return { context, fields, storage };
}

async function main() {
  const { context, fields, storage } = makeContext();
  const normalize = (value) => JSON.parse(JSON.stringify(value));
  vm.runInNewContext(fs.readFileSync('core/js/settings/audio.js', 'utf8'), context, {
    filename: 'core/js/settings/audio.js'
  });

  storage.set('audio_input_device_id', 'mic-1');
  storage.set('audio_output_device_id', 'speaker-1');
  assert.strictEqual(context.getPreferredAudioInputDeviceId(), 'mic-1');
  assert.strictEqual(context.getPreferredAudioOutputDeviceId(), 'speaker-1');

  await context.refreshAudioDevicePreferences(false);
  assert.deepStrictEqual(fields.audioInputDevice.options.map((option) => [option.value, option.textContent]), [
    ['', 'System default'],
    ['mic-1', 'Desk microphone'],
    ['mic-2', 'Microphone 2']
  ]);
  assert.deepStrictEqual(fields.audioOutputDevice.options.map((option) => [option.value, option.textContent]), [
    ['', 'System default'],
    ['speaker-1', 'Desk speakers']
  ]);
  assert.strictEqual(fields.audioInputDevice.value, 'mic-1');
  assert.strictEqual(fields.audioOutputDevice.value, 'speaker-1');

  storage.set('audio_input_device_id', 'missing-mic');
  storage.set('audio_output_device_id', 'missing-speaker');
  await context.refreshAudioDevicePreferences(false);
  assert.strictEqual(fields.audioInputDevice.value, '', 'missing input must fall back to system default');
  assert.strictEqual(fields.audioOutputDevice.value, '', 'missing output must fall back to system default');

  storage.set('audio_input_device_id', 'mic-2');
  assert.deepStrictEqual(normalize(context.getPreferredMicrophoneConstraints()), {
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      deviceId: { exact: 'mic-2' }
    }
  });
  storage.delete('audio_input_device_id');
  assert.deepStrictEqual(normalize(context.getPreferredMicrophoneConstraints()), {
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
  });

  storage.set('audio_input_device_id', 'mic-1');
  storage.set('audio_output_device_id', 'speaker-1');
  storage.set('tts_engine', 'openai');
  storage.set('tts_voice', 'nova');
  storage.set('tts_auto_speak', '1');
  storage.set('voice_endpoint_delay_ms', '3000');
  storage.set('live_translation_target', 'ko');
  storage.set('live_translation_model', 'lmstudio');
  context.initAudioPreferences();
  assert.strictEqual(fields.selEngine.value, 'openai');
  assert.strictEqual(fields.selVoice.value, 'nova');
  assert.strictEqual(fields.autoSpeak.checked, true);
  assert.strictEqual(fields.voiceEndpointDelay.value, '3000');
  assert.strictEqual(fields.liveTranslationTarget.value, 'ko');
  assert.strictEqual(fields.liveTranslationModel.value, 'lmstudio');

  fields.selEngine.value = 'local-voices';
  fields.selEngine.dispatch('change');
  assert.strictEqual(storage.get('tts_engine'), 'local-voices');
  fields.selVoice.value = 'alloy';
  fields.selVoice.dispatch('change');
  assert.strictEqual(storage.get('tts_voice'), 'alloy');
  fields.autoSpeak.checked = false;
  fields.autoSpeak.listeners.change();
  assert.strictEqual(storage.get('tts_auto_speak'), '0');
  fields.voiceEndpointDelay.value = '1500';
  fields.voiceEndpointDelay.dispatch('change');
  assert.strictEqual(context._vv.endpointDelayMs, 1500);
  assert.strictEqual(storage.get('voice_endpoint_delay_ms'), '1500');
  assert.deepStrictEqual(context._vv.endpoint.delays, [1500]);
  fields.liveTranslationTarget.value = 'es';
  fields.liveTranslationTarget.dispatch('change');
  assert.strictEqual(storage.get('live_translation_target'), 'es');
  assert.strictEqual(context.translationSyncCalls, 1);
  fields.liveTranslationModel.value = 'openai:gpt-4.1-nano';
  fields.liveTranslationModel.dispatch('change');
  assert.strictEqual(storage.get('live_translation_model'), 'openai:gpt-4.1-nano');

  storage.set('live_translation_target', 'invalid');
  storage.set('live_translation_model', 'invalid');
  assert.strictEqual(context.getLiveTranslationTarget(), 'en');
  assert.strictEqual(context.getLiveTranslationModel(), 'aig');
  fields.audioPlayback.sinkIds.length = 0;
  context._vv.audioCtx.sinkIds.length = 0;
  fields.audioInputDevice.value = 'mic-2';
  fields.audioInputDevice.dispatch('change');
  assert.strictEqual(storage.get('audio_input_device_id'), 'mic-2');
  assert.strictEqual(context.stopListeningCalls, 1);
  assert.strictEqual(context.startListeningCalls, 1);
  fields.audioOutputDevice.value = 'speaker-1';
  fields.audioOutputDevice.dispatch('change');
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(storage.get('audio_output_device_id'), 'speaker-1');
  assert.deepStrictEqual(fields.audioPlayback.sinkIds, ['speaker-1']);
  assert.deepStrictEqual(context._vv.audioCtx.sinkIds, ['speaker-1']);

  context.navigator.mediaDevices = {};
  await context.refreshAudioDevicePreferences(false);
  assert.strictEqual(fields.audioDeviceStatus.textContent, 'Audio device selection is unavailable in this browser.');

  console.log('audio settings tests: PASS');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});