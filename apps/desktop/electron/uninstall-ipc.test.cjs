'use strict'

const assert = require('node:assert/strict')
const test = require('node:test')

const { registerUninstallIpc } = require('./uninstall-ipc.cjs')

function fakeIpcMain() {
  const handlers = new Map()

  return {
    handlers,
    handle(channel, handler) {
      assert.ok(!handlers.has(channel), `duplicate registration for ${channel}`)
      handlers.set(channel, handler)
    }
  }
}

test('registerUninstallIpc wires only hermes:uninstall:* channels, each to a handler fn', () => {
  const ipcMain = fakeIpcMain()

  registerUninstallIpc({ ipcMain, getUninstallSummary: async () => ({}), runDesktopUninstall: async () => ({}) })

  assert.deepEqual([...ipcMain.handlers.keys()].sort(), ['hermes:uninstall:run', 'hermes:uninstall:summary'])

  for (const handler of ipcMain.handlers.values()) {
    assert.equal(typeof handler, 'function')
  }
})

test('run normalizes both the {mode} object form and the bare-string form', async () => {
  const ipcMain = fakeIpcMain()
  const modes = []

  registerUninstallIpc({
    ipcMain,
    getUninstallSummary: async () => ({}),
    runDesktopUninstall: async mode => {
      modes.push(mode)

      return { mode }
    }
  })

  await ipcMain.handlers.get('hermes:uninstall:run')({}, { mode: 'full' })
  await ipcMain.handlers.get('hermes:uninstall:run')({}, 'lite')
  await ipcMain.handlers.get('hermes:uninstall:run')({}, null)

  assert.deepEqual(modes, ['full', 'lite', ''])
})
