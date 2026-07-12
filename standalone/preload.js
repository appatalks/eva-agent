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
  egressMode: readArg('eva-egress-mode') || 'cloud',
  minimize: function() { ipcRenderer.send('win-minimize'); },
  maximize: function() { ipcRenderer.send('win-maximize'); },
  close: function() { ipcRenderer.send('win-close'); },
  authorizeAgentLaunch: function(agent, specification) {
    return ipcRenderer.invoke('eva-authorize-agent-launch', {
      agent: agent,
      specification: specification
    });
  }
}));
