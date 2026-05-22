'use strict';

// Sutando overlay app — minimal example shipping one overlay (System
// Resources) plus the generic overlay framework: a control server for the web
// UI manager, multi-display placement, auto-dim on app blur. Add an overlay by
// dropping an HTML file + renderer into this directory and registering it in
// OVERLAYS below.

const {
  app,
  BrowserWindow,
  ipcMain,
  globalShortcut,
  screen,
} = require('electron');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { getStats } = require('./stats');
const { startControlServer, stopControlServer } = require('./control-server');

const MARGIN = 20;
const DIM_OPACITY = 0.2; // opacity when the app loses focus (auto-dim)

let appDimmed = false; // true while overlays are dimmed (app unfocused)

// The overlay registry. Each entry is a controllable "overlay application"
// surfaced to the Sutando web UI via the control server. Add more by dropping
// a new HTML + renderer pair and another entry here.
const OVERLAYS = {
  resources: {
    name: 'System Resources',
    file: 'stats.html',
    w: 320,
    h: 420,
    shortcut: 'CommandOrControl+Shift+S',
    win: null,
    config: { opacity: 1, alwaysOnTop: true },
  },
};

// --- Multi-display support ----------------------------------------------

function resolveWorkspace() {
  const env = process.env.SUTANDO_WORKSPACE;
  if (env) return path.resolve(env.replace(/^~(?=$|\/)/, os.homedir()));
  return path.join(os.homedir(), '.sutando', 'workspace');
}

const DISPLAY_PREF_PATH = path.join(
  resolveWorkspace(),
  'state',
  'overlay-display.json'
);

let targetDisplayIndex = 1; // 1-based; 1 = primary

function loadDisplayPref() {
  try {
    const pref = JSON.parse(fs.readFileSync(DISPLAY_PREF_PATH, 'utf8'));
    if (Number.isInteger(pref.index) && pref.index >= 1) {
      targetDisplayIndex = pref.index;
    }
  } catch {
    /* no preference yet */
  }
}

function saveDisplayPref() {
  try {
    fs.mkdirSync(path.dirname(DISPLAY_PREF_PATH), { recursive: true });
    fs.writeFileSync(
      DISPLAY_PREF_PATH,
      JSON.stringify({ index: targetDisplayIndex })
    );
  } catch (err) {
    console.error('[main] display pref save failed:', err.message);
  }
}

function orderedDisplays() {
  const all = screen.getAllDisplays();
  const primary = screen.getPrimaryDisplay();
  const rest = all.filter((d) => d.id !== primary.id);
  return [primary, ...rest];
}

function currentDisplay() {
  const displays = orderedDisplays();
  return displays[targetDisplayIndex - 1] || displays[0];
}

function listDisplays() {
  return orderedDisplays().map((d, i) => ({
    index: i + 1,
    primary: i === 0,
    width: d.size.width,
    height: d.size.height,
    active: i + 1 === targetDisplayIndex,
  }));
}

// --- Window placement ----------------------------------------------------

function overlayPosition(id) {
  const { workArea } = currentDisplay();
  const o = OVERLAYS[id];
  // Default placement: top-right of the active display. Override per overlay
  // here when adding more.
  const x = workArea.x + workArea.width - o.w - MARGIN;
  const y = workArea.y + MARGIN;
  const maxX = workArea.x + workArea.width - o.w - 4;
  const maxY = workArea.y + workArea.height - o.h - 4;
  return {
    x: Math.round(Math.max(workArea.x + 4, Math.min(x, maxX))),
    y: Math.round(Math.max(workArea.y + 4, Math.min(y, maxY))),
  };
}

function openOverlay(id) {
  const o = OVERLAYS[id];
  if (!o) throw new Error(`unknown overlay: ${id}`);
  if (o.win && !o.win.isDestroyed()) {
    o.win.show();
    o.win.moveTop();
    return;
  }
  const pos = overlayPosition(id);
  o.win = new BrowserWindow({
    width: o.w,
    height: o.h,
    x: pos.x,
    y: pos.y,
    show: false,
    frame: false,
    transparent: true,
    resizable: true,
    alwaysOnTop: o.config.alwaysOnTop,
    skipTaskbar: true,
    fullscreenable: false,
    hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  o.win.loadFile(o.file);
  o.win.once('ready-to-show', () => {
    applyConfig(id, o.config);
    o.win.show();
    o.win.moveTop();
  });
  o.win.on('closed', () => {
    o.win = null;
  });
}

function closeOverlay(id) {
  const o = OVERLAYS[id];
  if (o && o.win && !o.win.isDestroyed()) {
    // destroy() is synchronous; close() is async and would leave list()
    // reporting stale state in the control-server response.
    o.win.destroy();
    o.win = null;
  }
}

function showOverlay(id) {
  const o = OVERLAYS[id];
  if (o && o.win && !o.win.isDestroyed()) {
    o.win.show();
    o.win.moveTop();
  }
}

function hideOverlay(id) {
  const o = OVERLAYS[id];
  if (o && o.win && !o.win.isDestroyed()) o.win.hide();
}

// Effective opacity = dim level while unfocused, else the overlay's config.
function effectiveOpacity(o) {
  return appDimmed ? DIM_OPACITY : o.config.opacity;
}

function applyOpacityAll() {
  for (const o of Object.values(OVERLAYS)) {
    if (o.win && !o.win.isDestroyed()) o.win.setOpacity(effectiveOpacity(o));
  }
}

function applyConfig(id, cfg) {
  const o = OVERLAYS[id];
  if (!o) throw new Error(`unknown overlay: ${id}`);
  if (typeof cfg.opacity === 'number') {
    o.config.opacity = Math.max(0.2, Math.min(1, cfg.opacity));
    if (o.win && !o.win.isDestroyed()) o.win.setOpacity(effectiveOpacity(o));
  }
  if (typeof cfg.alwaysOnTop === 'boolean') {
    o.config.alwaysOnTop = cfg.alwaysOnTop;
    if (o.win && !o.win.isDestroyed()) {
      o.win.setAlwaysOnTop(o.config.alwaysOnTop, 'screen-saver');
    }
  }
  return o.config;
}

function moveAllToDisplay(index) {
  const displays = orderedDisplays();
  if (!Number.isInteger(index) || index < 1 || index > displays.length) {
    return {
      ok: false,
      error: `display ${index} not found — ${displays.length} display(s) connected`,
      displays: listDisplays(),
    };
  }
  targetDisplayIndex = index;
  saveDisplayPref();
  for (const id of Object.keys(OVERLAYS)) {
    const o = OVERLAYS[id];
    if (o.win && !o.win.isDestroyed()) {
      const pos = overlayPosition(id);
      o.win.setBounds({ x: pos.x, y: pos.y, width: o.w, height: o.h });
      o.win.moveTop();
    }
  }
  return { ok: true, display: index, displays: listDisplays() };
}

function listOverlays() {
  return Object.entries(OVERLAYS).map(([id, o]) => {
    const live = !!(o.win && !o.win.isDestroyed());
    return {
      id,
      name: o.name,
      open: live,
      visible: live && o.win.isVisible(),
      bounds: live ? o.win.getBounds() : null,
      config: o.config,
    };
  });
}

const overlayManager = {
  list: listOverlays,
  open: openOverlay,
  close: closeOverlay,
  show: showOverlay,
  hide: hideOverlay,
  setConfig: applyConfig,
  displays: listDisplays,
  moveToDisplay: moveAllToDisplay,
};

// --- IPC -----------------------------------------------------------------

ipcMain.handle('stats:get', async () => {
  try {
    return { ok: true, data: await getStats() };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.on('window:close', () => app.quit());
ipcMain.on('window:hide', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (win) win.hide();
});

app.whenReady().then(() => {
  loadDisplayPref();
  for (const id of Object.keys(OVERLAYS)) openOverlay(id);

  for (const [id, o] of Object.entries(OVERLAYS)) {
    globalShortcut.register(o.shortcut, () => {
      const w = OVERLAYS[id].win;
      if (!w || w.isDestroyed()) openOverlay(id);
      else if (w.isVisible()) w.hide();
      else showOverlay(id);
    });
  }

  startControlServer(overlayManager);

  // Auto-dim on app blur; restore on focus. Deferred blur check avoids a brief
  // dim when clicking between overlays.
  app.on('browser-window-focus', () => {
    if (appDimmed) {
      appDimmed = false;
      applyOpacityAll();
    }
  });
  app.on('browser-window-blur', () => {
    setTimeout(() => {
      if (!appDimmed && !BrowserWindow.getFocusedWindow()) {
        appDimmed = true;
        applyOpacityAll();
      }
    }, 80);
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      for (const id of Object.keys(OVERLAYS)) openOverlay(id);
    }
  });
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopControlServer();
});
app.on('window-all-closed', () => {
  // Overlays can all be closed via the manager without quitting the app.
});
