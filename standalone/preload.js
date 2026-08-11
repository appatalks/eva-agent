const { contextBridge, ipcRenderer } = require('electron');

function readArg(name) {
  const prefix = '--' + name + '=';
  const arg = process.argv.find(function(value) {
    return value.indexOf(prefix) === 0;
  });
  return arg ? arg.slice(prefix.length) : '';
}

contextBridge.exposeInMainWorld('evaStandalone', Object.freeze({
  acpBaseUrl: readArg('eva-acp-base-url'),
  bridgeToken: ipcRenderer.sendSync('bridge-capability-token'),
  isStandalone: true,
  version: readArg('eva-version'),
  workspaceTerminalV1: readArg('eva-workspace-terminal-v1') === '1',
  terminalAssets: Object.freeze({
    xterm: readArg('eva-xterm-url'),
    css: readArg('eva-xterm-css-url'),
    fit: readArg('eva-xterm-fit-url'),
    search: readArg('eva-xterm-search-url'),
    webLinks: readArg('eva-xterm-web-links-url')
  }),
  terminalList: function() { return ipcRenderer.invoke('terminal-list'); },
  terminalCreate: function(request) { return ipcRenderer.invoke('terminal-create', request); },
  terminalReplay: function(id) { return ipcRenderer.invoke('terminal-replay', id); },
  terminalWrite: function(id, data) { return ipcRenderer.invoke('terminal-write', id, data); },
  terminalResize: function(id, cols, rows) { return ipcRenderer.invoke('terminal-resize', id, cols, rows); },
  terminalClose: function(id) { return ipcRenderer.invoke('terminal-close', id); },
  terminalCloseRoot: function(rootId) { return ipcRenderer.invoke('terminal-close-root', rootId); },
  onTerminalData: function(listener) {
    const wrapped = function(_event, payload) { listener(payload); };
    ipcRenderer.on('terminal:data', wrapped);
    return function() { ipcRenderer.removeListener('terminal:data', wrapped); };
  },
  onTerminalExit: function(listener) {
    const wrapped = function(_event, payload) { listener(payload); };
    ipcRenderer.on('terminal:exit', wrapped);
    return function() { ipcRenderer.removeListener('terminal:exit', wrapped); };
  },
  workspaceListProjects: function() { return ipcRenderer.invoke('workspace-list-projects'); },
  workspaceSelectProject: function() { return ipcRenderer.invoke('workspace-select-project'); },
  workspaceImportGitHub: function(repositoryUrl) { return ipcRenderer.invoke('workspace-import-github', repositoryUrl); },
  workspaceSetMcpServer: function(projectId, serverName, enabled, approvedDigest) { return ipcRenderer.invoke('workspace-set-mcp-server', projectId, serverName, enabled, approvedDigest); },
  workspaceCreateRun: function(request) { return ipcRenderer.invoke('workspace-create-run', request); },
  workspaceListRuns: function(projectId) { return ipcRenderer.invoke('workspace-list-runs', projectId); },
  workspaceListProjectFiles: function(projectId) { return ipcRenderer.invoke('workspace-list-project-files', projectId); },
  workspaceOpenProjectFile: function(projectId, relativePath) { return ipcRenderer.invoke('workspace-open-project-file', projectId, relativePath); },
  workspaceCheckoutStatus: function(checkoutId) { return ipcRenderer.invoke('workspace-checkout-status', checkoutId); },
  workspaceListAssets: function() { return ipcRenderer.invoke('workspace-list-assets'); },
  workspaceOpenAsset: function(runId, relativePath) { return ipcRenderer.invoke('workspace-open-asset', runId, relativePath); },
  workspaceRunAction: function(runId, action, options) { return ipcRenderer.invoke('workspace-run-action', runId, action, options); },
  authLoad: function() { return ipcRenderer.invoke('auth-load'); },
  authSave: function(values) { return ipcRenderer.invoke('auth-save', values); },
  minimize: function() { ipcRenderer.send('win-minimize'); },
  maximize: function() { ipcRenderer.send('win-maximize'); },
  close: function() { ipcRenderer.send('win-close'); },
  localVoicesStatus: function() { return ipcRenderer.invoke('local-voices-status'); },
  localVoicesStart: function(pythonPath, voiceId) { return ipcRenderer.invoke('local-voices-start', pythonPath, voiceId); },
  localVoicesStop: function() { return ipcRenderer.invoke('local-voices-stop'); },
  localVoicesList: function() { return ipcRenderer.invoke('local-voices-list'); },
  localVoicesImport: function() { return ipcRenderer.invoke('local-voices-import'); },
  localSpeechSynthesize: function(request) { return ipcRenderer.invoke('local-speech-synthesize', request); },
  localSpeechAcknowledgement: function(request) { return ipcRenderer.invoke('local-speech-acknowledgement', request); },
  localSpeechWarmAcknowledgements: function(request) { return ipcRenderer.invoke('local-speech-warm-acknowledgements', request); },
  localSpeechTranscribe: function(audio, contentType, language, liveTranslation) { return ipcRenderer.invoke('local-speech-transcribe', audio, contentType, language, liveTranslation); },
  localSpeechWarmTranslation: function(multilingual) { return ipcRenderer.invoke('local-speech-warm-translation', !!multilingual); }
}));
