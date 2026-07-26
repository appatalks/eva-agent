// profiles.js - local user profiles for browser state and sessions.
// API credentials, bridge location, MCP configuration, and installation state stay shared.

var EVA_PROFILES_KEY = 'eva_profiles';
var EVA_ACTIVE_PROFILE_KEY = 'eva_active_profile';
var EVA_PROFILE_PREFIX = 'eva_profile:';
var EVA_DEFAULT_PROFILE = { id: 'appatalks', name: 'appatalks' };

function _profileSharedKey(key) {
  return key === EVA_PROFILES_KEY || key === EVA_ACTIVE_PROFILE_KEY ||
    key.indexOf(EVA_PROFILE_PREFIX) === 0 || key.indexOf('auth_') === 0 ||
    key === 'mcp_config' || key === 'acp_bridge_url' ||
    key === 'eva_memory_backend' || key === 'eva_standalone_first_run_done';
}

function getEvaProfiles() {
  try {
    var profiles = JSON.parse(localStorage.getItem(EVA_PROFILES_KEY) || '[]');
    if (Array.isArray(profiles) && profiles.length) return profiles;
  } catch (error) {}
  localStorage.setItem(EVA_PROFILES_KEY, JSON.stringify([EVA_DEFAULT_PROFILE]));
  return [EVA_DEFAULT_PROFILE];
}

function getActiveEvaProfileId() {
  var profiles = getEvaProfiles();
  var active = localStorage.getItem(EVA_ACTIVE_PROFILE_KEY) || EVA_DEFAULT_PROFILE.id;
  if (!profiles.some(function(profile) { return profile.id === active; })) active = profiles[0].id;
  localStorage.setItem(EVA_ACTIVE_PROFILE_KEY, active);
  return active;
}

function getActiveEvaProfile() {
  var active = getActiveEvaProfileId();
  return getEvaProfiles().filter(function(profile) { return profile.id === active; })[0] || EVA_DEFAULT_PROFILE;
}

function _profileStorageKey(profileId, key) {
  return EVA_PROFILE_PREFIX + profileId + ':' + key;
}

function saveEvaProfileState(profileId) {
  var prefix = EVA_PROFILE_PREFIX + profileId + ':';
  var previousKeys = [];
  for (var previousIndex = 0; previousIndex < localStorage.length; previousIndex++) {
    var previousKey = localStorage.key(previousIndex);
    if (previousKey && previousKey.indexOf(prefix) === 0) previousKeys.push(previousKey);
  }
  previousKeys.forEach(function(key) { localStorage.removeItem(key); });
  var keys = [];
  for (var index = 0; index < localStorage.length; index++) keys.push(localStorage.key(index));
  keys.forEach(function(key) {
    if (!key || _profileSharedKey(key)) return;
    localStorage.setItem(prefix + key, localStorage.getItem(key));
  });
  localStorage.setItem(_profileStorageKey(profileId, '_initialized'), '1');
}

function _clearEvaProfileState() {
  var keys = [];
  for (var index = 0; index < localStorage.length; index++) keys.push(localStorage.key(index));
  keys.forEach(function(key) {
    if (key && !_profileSharedKey(key)) localStorage.removeItem(key);
  });
}

function restoreEvaProfileState(profileId) {
  _clearEvaProfileState();
  var prefix = EVA_PROFILE_PREFIX + profileId + ':';
  var values = [];
  for (var index = 0; index < localStorage.length; index++) {
    var key = localStorage.key(index);
    if (key && key.indexOf(prefix) === 0) values.push([key.slice(prefix.length), localStorage.getItem(key)]);
  }
  values.forEach(function(pair) {
    if (pair[0] !== '_initialized') localStorage.setItem(pair[0], pair[1]);
  });
}

async function switchEvaProfile(profileId) {
  if (profileId === getActiveEvaProfileId()) return;
  if (!getEvaProfiles().some(function(profile) { return profile.id === profileId; })) return;
  if (typeof saveCurrentSession === 'function') await saveCurrentSession();
  saveEvaProfileState(getActiveEvaProfileId());
  localStorage.setItem(EVA_ACTIVE_PROFILE_KEY, profileId);
  restoreEvaProfileState(profileId);
  location.reload();
}

async function addEvaProfile() {
  var name = await evaTextPrompt('Profile name', '', { maxLength: 40, placeholder: 'Name' });
  if (!name) return;
  name = name.trim().slice(0, 40);
  if (!name) return;
  var profiles = getEvaProfiles();
  var id = 'profile_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6);
  profiles.push({ id: id, name: name });
  localStorage.setItem(EVA_PROFILES_KEY, JSON.stringify(profiles));
  renderEvaProfiles();
}

function deleteEvaProfile(profileId) {
  if (profileId === getActiveEvaProfileId()) return;
  var profiles = getEvaProfiles();
  var profile = profiles.filter(function(item) { return item.id === profileId; })[0];
  if (!profile || !confirm('Delete profile "' + profile.name + '" and its local settings?')) return;
  profiles = profiles.filter(function(item) { return item.id !== profileId; });
  localStorage.setItem(EVA_PROFILES_KEY, JSON.stringify(profiles));
  var prefix = EVA_PROFILE_PREFIX + profileId + ':';
  var keys = [];
  for (var index = 0; index < localStorage.length; index++) {
    var key = localStorage.key(index);
    if (key && key.indexOf(prefix) === 0) keys.push(key);
  }
  keys.forEach(function(key) { localStorage.removeItem(key); });
  renderEvaProfiles();
}

function renderEvaProfiles() {
  var active = getActiveEvaProfileId();
  var nameEl = document.getElementById('evaActiveProfileName');
  var activeProfile = getActiveEvaProfile();
  if (nameEl) nameEl.textContent = activeProfile.name;
  var list = document.getElementById('profileList');
  if (!list) return;
  list.textContent = '';
  getEvaProfiles().forEach(function(profile) {
    var item = document.createElement('li');
    item.className = 'session-item' + (profile.id === active ? ' active' : '');
    var title = document.createElement('span');
    title.className = 'session-title';
    title.textContent = profile.name;
    item.appendChild(title);
    var actions = document.createElement('span');
    actions.className = 'session-actions';
    if (profile.id !== active) {
      var select = document.createElement('button');
      select.className = 'auth-save background-inline-button';
      select.textContent = 'Switch';
      select.addEventListener('click', function(event) { event.stopPropagation(); switchEvaProfile(profile.id); });
      actions.appendChild(select);
      var remove = document.createElement('button');
      remove.className = 'auth-toggle';
      remove.textContent = 'Delete';
      remove.addEventListener('click', function(event) { event.stopPropagation(); deleteEvaProfile(profile.id); });
      actions.appendChild(remove);
    } else {
      var current = document.createElement('span');
      current.className = 'session-time';
      current.textContent = 'Current';
      actions.appendChild(current);
    }
    item.appendChild(actions);
    item.addEventListener('click', function() { if (profile.id !== active) switchEvaProfile(profile.id); });
    list.appendChild(item);
  });
}

function toggleProfilePanel(force) {
  var panel = document.getElementById('profilePanel');
  if (!panel) return;
  var visible = panel.getAttribute('aria-hidden') !== 'true';
  var shouldOpen = typeof force === 'boolean' ? force : !visible;
  if (shouldOpen && typeof closeAgentOperationsForNavigation === 'function') closeAgentOperationsForNavigation();
  if (shouldOpen && typeof closeSidePanels === 'function') closeSidePanels('profilePanel');
  panel.setAttribute('aria-hidden', shouldOpen ? 'false' : 'true');
  if (shouldOpen) renderEvaProfiles();
}

function initEvaProfiles() {
  var active = getActiveEvaProfileId();
  if (!localStorage.getItem(_profileStorageKey(active, '_initialized'))) saveEvaProfileState(active);
  renderEvaProfiles();
  var userButton = document.getElementById('evaUserBtn');
  if (userButton) userButton.addEventListener('click', function(event) { event.stopPropagation(); toggleProfilePanel(); });
  var closeButton = document.getElementById('profilePanelClose');
  if (closeButton) closeButton.addEventListener('click', function() { toggleProfilePanel(false); });
  var addButton = document.getElementById('profileAddBtn');
  if (addButton) addButton.addEventListener('click', addEvaProfile);
}

document.addEventListener('DOMContentLoaded', initEvaProfiles);
