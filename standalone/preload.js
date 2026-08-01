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
  localSpeechSynthesize: function(text) { return ipcRenderer.invoke('local-speech-synthesize', text); },
  localSpeechTranscribe: function(audio, contentType) { return ipcRenderer.invoke('local-speech-transcribe', audio, contentType); }
}));
