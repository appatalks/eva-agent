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
  isStandalone: true,
  version: readArg('eva-version'),
  minimize: function() { ipcRenderer.send('win-minimize'); },
  maximize: function() { ipcRenderer.send('win-maximize'); },
  close: function() { ipcRenderer.send('win-close'); },
  localVoicesStatus: function(baseUrl) { return ipcRenderer.invoke('local-voices-status', baseUrl); },
  localVoicesStart: function(baseUrl, pythonPath, voiceId) { return ipcRenderer.invoke('local-voices-start', baseUrl, pythonPath, voiceId); },
  localVoicesStop: function(baseUrl) { return ipcRenderer.invoke('local-voices-stop', baseUrl); },
  localVoicesList: function() { return ipcRenderer.invoke('local-voices-list'); },
  localVoicesImport: function() { return ipcRenderer.invoke('local-voices-import'); }
}));
