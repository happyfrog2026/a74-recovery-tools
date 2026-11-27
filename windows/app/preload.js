const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('camera', Object.freeze({
  openSupport: model => ipcRenderer.invoke('open-support', model),
  openFirmwareArchive: model => ipcRenderer.invoke('open-firmware-archive', model),
  fastbootDriverStatus: () => ipcRenderer.invoke('fastboot-driver-status'),
  openFastbootDriver: () => ipcRenderer.invoke('open-fastboot-driver'),
  removeFastbootWinUsb: language => ipcRenderer.invoke('remove-fastboot-winusb', language),
  paths: () => ipcRenderer.invoke('paths'),
  choose: kind => ipcRenderer.invoke('choose', kind),
  backupDestination: selected => ipcRenderer.invoke('backup-destination', selected),
  resolveSnapshot: selected => ipcRenderer.invoke('resolve-snapshot', selected),
  readLogs: () => ipcRenderer.invoke('read-logs'),
  confirm: (title, detail, language) => ipcRenderer.invoke('confirm', title, detail, language),
  run: request => ipcRenderer.invoke('worker', request),
  exportLogs: taskDirs => ipcRenderer.invoke('export-logs', taskDirs),
  onProgress: callback => {
    const listener = (_event, detail) => callback(detail);
    ipcRenderer.on('worker-progress', listener);
    return () => ipcRenderer.removeListener('worker-progress', listener);
  }
}));
