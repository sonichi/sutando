---
name: electron-overlay-dimming
description: Reusable pattern for focus-based auto-dimming of Electron overlay windows — when the app loses focus, all overlay windows fade to a low opacity; when an overlay regains focus, they return to their configured opacity. Use when building always-on-top Electron overlays that should recede while the user works in other apps.
---

# Electron Overlay Auto-Dimming

A small, self-contained pattern for always-on-top Electron overlays: the
overlays stay readable while in use but **fade out of the way** when the user
clicks into another application, and **restore** when focus returns to an
overlay. First shipped in the `benchmark-overlay` app.

## Behaviour

- App loses focus (no overlay window is focused) → every overlay window's
  opacity drops to a dim level (~0.2).
- Focus returns to any overlay → every overlay restores to its *configured*
  opacity (not blindly to 1.0 — it respects any per-overlay opacity the user
  set).

## Why it's not naive

Two pitfalls the pattern handles:

1. **Inter-overlay clicks.** Clicking from overlay A to overlay B fires a
   `blur` then a `focus`. Dimming on raw `blur` would flicker. The fix: on
   `blur`, defer ~80 ms and only dim if `BrowserWindow.getFocusedWindow()` is
   then `null` — i.e. the *app* truly lost focus, not just one window.
2. **Configured opacity.** Overlays may each have a user-set base opacity.
   Auto-dim must dim *from* and restore *to* that value, so opacity is always
   computed as `appDimmed ? DIM_OPACITY : overlay.config.opacity`.

## Reference implementation (main process)

```js
const DIM_OPACITY = 0.2;
let appDimmed = false;

function effectiveOpacity(o) {
  return appDimmed ? DIM_OPACITY : o.config.opacity;
}

function applyOpacityAll() {
  for (const o of Object.values(OVERLAYS)) {
    if (o.win && !o.win.isDestroyed()) o.win.setOpacity(effectiveOpacity(o));
  }
}

app.on('browser-window-focus', () => {
  if (appDimmed) { appDimmed = false; applyOpacityAll(); }
});

app.on('browser-window-blur', () => {
  // Defer so an A→B overlay click doesn't briefly dim.
  setTimeout(() => {
    if (!appDimmed && !BrowserWindow.getFocusedWindow()) {
      appDimmed = true;
      applyOpacityAll();
    }
  }, 80);
});
```

Any code path that sets a window's opacity (e.g. a config handler) must route
through `effectiveOpacity()` so opacity changes made while dimmed don't undim
the window.

## Reference app

Live implementation: `~/projects/benchmark-overlay/main.js` — the
`benchmark-overlay` app applies this across three overlay windows (AI
Benchmarks, System Resources, Hub Overlay).
