#!/usr/bin/env node
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('index.html', 'utf8');
const scriptSources = [...html.matchAll(/<script src="([^"]+)"/g)].map((match) => match[1].split('?')[0]);

function scriptIndex(path) {
  const index = scriptSources.indexOf(path);
  assert.notStrictEqual(index, -1, `${path} must be loaded by index.html`);
  return index;
}

const optionsIndex = scriptIndex('core/js/options.js');
[
  'core/js/model-routing.js',
  'core/js/runtime/bridge-client.js',
  'core/js/settings/model-settings.js',
  'core/js/settings/prompts.js',
  'core/js/settings/goals.js',
  'core/js/settings/runtime.js',
  'core/js/settings/cron.js',
  'core/js/settings/background.js',
  'core/js/settings/alerts.js',
  'core/js/settings/audio.js',
  'core/js/features/skills/auto-learn.js',
  'core/js/features/notifications/proactive.js',
  'core/js/features/permissions/acp.js'
].forEach((path) => {
  assert.ok(scriptIndex(path) < optionsIndex, `${path} must load before options.js`);
});

assert.ok(scriptIndex('core/js/features/automation/browser-agent.js') > optionsIndex,
  'browser automation controller must load after options.js because it consumes shared UI helpers');
assert.ok(scriptIndex('core/js/features/automation/camera.js') > optionsIndex,
  'camera controller must load after options.js because it consumes shared auth and bridge helpers');
assert.ok(scriptIndex('core/js/features/assets/library.js') > optionsIndex,
  'Assets library must load after options.js because it consumes shared bridge helpers');
assert.ok(scriptIndex('core/js/features/agents/operations.js') > optionsIndex,
  'Agent Operations must load after options.js because it consumes shared navigation helpers');
assert.ok(scriptIndex('core/js/skills.js') > optionsIndex,
  'skills.js must load after options.js because it consumes shared bridge helpers');

const extractedSources = [
  'core/js/model-routing.js',
  'core/js/runtime/bridge-client.js',
  'core/js/settings/model-settings.js',
  'core/js/settings/prompts.js',
  'core/js/settings/goals.js',
  'core/js/settings/runtime.js',
  'core/js/settings/cron.js',
  'core/js/settings/background.js',
  'core/js/settings/alerts.js',
  'core/js/settings/audio.js',
  'core/js/features/skills/auto-learn.js',
  'core/js/features/notifications/proactive.js',
  'core/js/features/permissions/acp.js'
];
const sandbox = {
  AbortSignal: { timeout() { return {}; } },
  JSON,
  Promise,
  Set,
  Number,
  String,
  Date,
  console
};
sandbox.window = sandbox;
extractedSources.forEach((path) => {
  vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path });
});
assert.ok(sandbox.EvaModelRouting, 'model routing module must export EvaModelRouting');
assert.ok(sandbox.PERSONALITY_PRESETS, 'prompt settings must export PERSONALITY_PRESETS');
assert.ok(sandbox._STALE_PRESETS, 'prompt settings must export _STALE_PRESETS');
[
  'getSystemPrompt',
  'applyPersonalityPreset',
  'initSystemPrompt',
  'backgroundBridgeRequest',
  'getModelMaxTokens',
  'initGoals',
  'switchDataMode',
  'cronAdd',
  'initBackground',
  'initAlerts',
  'getLiveTranslationTarget',
  'getLiveTranslationModel',
  'getPreferredAudioInputDeviceId',
  'getPreferredAudioOutputDeviceId',
  'refreshAudioDevicePreferences',
  'applyPreferredAudioOutputDevice',
  'getPreferredMicrophoneConstraints',
  'initAudioDevicePreferences',
  'initAudioPreferences',
  'autoLearnSkill',
  'initNotifications',
  'initACPPermissions'
].forEach((name) => {
  assert.strictEqual(typeof sandbox[name], 'function', `${name} must remain globally available`);
});

const optionsSource = fs.readFileSync('core/js/options.js', 'utf8');
assert.ok(!optionsSource.includes('function initBackground()'),
  'options.js must not shadow the extracted Background owner');
assert.ok(!optionsSource.includes('var _backgroundState ='),
  'options.js must not recreate extracted Background state');
assert.ok(!optionsSource.includes('function initAlerts()'),
  'options.js must not shadow the extracted Alerts owner');
assert.ok(!optionsSource.includes('var _alertsState ='),
  'options.js must not recreate extracted Alerts state');
assert.ok(!optionsSource.includes('function initNotifications()'),
  'options.js must not shadow the proactive Notifications owner');
assert.ok(!optionsSource.includes('var _notifState ='),
  'options.js must not recreate proactive Notifications state');
assert.ok(!optionsSource.includes('function initACPPermissions()'),
  'options.js must not shadow the extracted ACP permission owner');
assert.ok(!optionsSource.includes('var _acpPermissionState ='),
  'options.js must not recreate extracted ACP permission state');
assert.ok(!optionsSource.includes('var PERSONALITY_PRESETS ='),
  'options.js must not shadow the extracted prompt presets owner');
assert.ok(!optionsSource.includes('function getSystemPrompt()'),
  'options.js must not shadow the extracted system prompt owner');
assert.ok(!optionsSource.includes('function applyPersonalityPreset()'),
  'options.js must not shadow the extracted personality preset owner');
assert.ok(!optionsSource.includes('var _STALE_PRESETS ='),
  'options.js must not recreate stale prompt migration state');
assert.ok(!optionsSource.includes('function initSystemPrompt()'),
  'options.js must not shadow the extracted prompt initialization owner');

[
  'getLiveTranslationTarget',
  'getLiveTranslationModel',
  'getPreferredAudioInputDeviceId',
  'getPreferredAudioOutputDeviceId',
  '_audioDeviceStatus',
  '_addAudioDeviceOptions',
  'refreshAudioDevicePreferences',
  'applyPreferredAudioOutputDevice',
  'getPreferredMicrophoneConstraints',
  'initAudioDevicePreferences',
  'initAudioPreferences'
].forEach((name) => {
  assert.ok(!optionsSource.includes(`function ${name}(`),
    `options.js must not shadow the extracted Audio owner: ${name}`);
});

console.log(`frontend script-order tests: PASS (${scriptSources.length} scripts)`);