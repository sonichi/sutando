# Pointer Teacher — v1 Tracer Bullet (now productionized)

> **Status:** this tracer did its job. The loop is now folded into the live app
> ([design §6](../docs/pointer-teacher-design.md#6-v1--productionized-into-sutando-)):
> `resolver.py` → the **`point_at` inline tool** (`src/browser-tools.ts`);
> `pointer-overlay.swift` → **`PointerOverlayView`** in `src/Sutando/main.swift`
> driven by the app's `DispatchSource` watch of `<workspace>/state/pointer-cmd.json`.
> Kept here as the proof artifact and the reference the production port was lifted from.

Thinnest end-to-end slice of the [Pointer Teacher design](../docs/pointer-teacher-design.md) §6.
Proves the whole embodied loop on one real example **without touching the live
`Sutando.app` / voice agent** — useful as a standalone repro even now.

## What it exercises (every layer, end to end)

```
intent string
  → resolver.py   capture via :7845  →  gemini-3-flash-preview
                  (native [y,x] 0-1000 format, thinking off)      [ADR-0001]
  → /tmp/pointer-cmd.json   (IPC: {nx,ny,label,say,ts})
  → pointer-overlay (Swift) screenSaver/click-through window,
                  Clicky-style quadratic-bezier flight to the Target
  → say            spoken narration (tracer stand-in for bodhi TTS)
```

## Run

```bash
./run.sh "where do I commit here?"     # focus VSCode first
./run.sh "the Search icon"
./run.sh "the file CLAUDE.md"
```

First run builds + launches the overlay; subsequent runs just re-resolve.

## Deliberate scope guards (per §6)

- **Reactive mode only** — no Lesson Spine, no knowledge journal.
- **Vision path only** — AX-first is the proven-next layer (design open item #3);
  the POC proved the vision path, so the tracer rides it.
- **Single / main display** — multi-display coordinate transforms out of scope.
- **`say` for narration** — real narration is the bodhi voice agent.
- **Standalone overlay binary** — production target is the `Sutando.app`
  `hudWindow` (`src/Sutando/main.swift`); kept separate here to avoid
  destabilising the running menubar app while de-risking.

## Production follow-ups (from the design doc)

1. Fold the resolver into a `point_at` **inline tool** (`src/browser-tools.ts`).
2. Port the bezier overlay into `Sutando.app`'s `hudWindow`; replace the
   file-poll IPC with the app's existing `DispatchSource` channel.
3. Add AX-first resolution (open item #3) ahead of the vision call.
4. Register `point_at` with the voice agent + the arming hotkey.
5. Latency: paid key / caching / image-size tuning (open item #1).
