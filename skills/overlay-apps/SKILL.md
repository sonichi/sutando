---
name: overlay-apps
description: Framework + minimal example for Sutando desktop overlay applications — always-on-top, frameless Electron windows that float over the desktop, controllable from the Sutando web UI's /overlays manager. Ships one example overlay (System Resources). Add new overlays by registering them in app/main.js.
---

# Overlay Apps

A small framework for **Sutando desktop overlays**: always-on-top, frameless,
transparent Electron windows that float over whatever you're working on,
controllable from the web UI's `/overlays` manager view.

The framework gives you, for free:

- A localhost **control server** the web UI manager talks to
  (open / close / show / hide / opacity / always-on-top).
- **Multi-display placement** — move every overlay to a chosen monitor; the
  choice persists across restarts.
- **Auto-dim on app blur** — overlays fade to ~20% opacity when you click into
  another app, restore to their configured opacity when you click an overlay
  back.
- A simple `OVERLAYS` registry — add a new overlay by registering it.

## Ships with

One example overlay: **System Resources** — live CPU / memory / disk / network
/ load (Cmd+Shift+S). The framework is overlay-agnostic; the example is just a
working starting point.

## Layout — workspace contract

- **Source of truth:** `skills/overlay-apps/app/` (in the repo).
- **Running instance:** `<workspace>/overlay-apps/benchmark-overlay/`
  — `node_modules` and any local state live here, not in the repo. Code in the
  repo, mutable runtime in the workspace, per the Sutando workspace contract.
  `<workspace>` resolves via `bash scripts/sutando-config.sh workspace` (M0
  helper, PR #1395) — defaults to `<repo>/workspace/`.

`scripts/launch.sh` syncs source → workspace, installs dependencies, and
starts the app.

## Launch

```bash
bash skills/overlay-apps/scripts/launch.sh
```

Requires Node.js.

## Adding a new overlay

1. Drop in `app/<your>.html` and `app/<your>-renderer.js`.
2. Register it in `OVERLAYS` in `app/main.js`:
   ```js
   yourId: {
     name: 'Your Overlay',
     file: 'your.html',
     w: 320, h: 380,
     shortcut: 'CommandOrControl+Shift+Y',
     win: null,
     config: { opacity: 1, alwaysOnTop: true },
   },
   ```
3. If your overlay needs data, add an IPC handler in `main.js` and expose it
   via `preload.js`; renderers call `window.overlay.<your-method>()`.

That's it — control-server, manager-UI, multi-display and auto-dim all pick it
up automatically.

## Control surface

The app runs a localhost control server (port 7849+) and writes a discovery
file to `<workspace>/state/overlay-control.json`. The web UI's
`/overlays` view proxies to it. Endpoints:

| Method | Path                                    | Purpose                              |
|--------|-----------------------------------------|--------------------------------------|
| GET    | `/overlays`                             | list overlays + state + bounds       |
| GET    | `/displays`                             | connected monitors                   |
| POST   | `/overlays/:id/{open,close,show,hide}`  | window lifecycle                     |
| POST   | `/overlays/:id/config`                  | `{opacity, alwaysOnTop}`             |
| POST   | `/overlays/display`                     | `{index}` — move all to a display    |
