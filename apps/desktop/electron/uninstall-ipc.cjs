'use strict'

// Uninstall IPC: summarize what a desktop uninstall would remove + run it
// (GUI-only / lite / full). Both delegate to the main-process uninstall engine,
// which is injected.
function registerUninstallIpc({ getUninstallSummary, ipcMain, runDesktopUninstall }) {
  ipcMain.handle('hermes:uninstall:summary', async () => getUninstallSummary())
  ipcMain.handle('hermes:uninstall:run', async (_event, payload) => {
    const mode = payload && typeof payload === 'object' ? payload.mode : payload
    return runDesktopUninstall(String(mode || ''))
  })
}

module.exports = { registerUninstallIpc }
