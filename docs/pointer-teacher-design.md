# Pointer Teacher — Design

Bring Clicky's embodied "learn by doing" mechanism into Sutando: an on-screen guide that watches the user's real screen, talks, and **flies a marker to the thing it's teaching**. Domain vocabulary: [`CONTEXT.md`](../CONTEXT.md). Pointer-brain decision: [ADR-0001](./adr/0001-pointer-teacher-brain.md). Empirical findings memory: `project_pointer_teacher_ax_findings.md`.

## 1. What we are bringing (and why)

Clicky's soul is not Q&A — it is *teaching happens while you work, in the real software, with a patient expert pointing over your shoulder.* The embodied pointing + voice is the differentiator (Farza's demo: 3M views, 5.7k stars). Scope is **general** ("learn anything by doing" — DaVinci, Figma, a codebase, anything on screen); "teach me the Sutando codebase in VSCode" is one *example*, not the boundary.

## 2. Decision log (resolved via /grill-with-docs)

| Branch | Decision | Why |
|---|---|---|
| Teacher model | **Reactive companion** core; Structured (Lesson Spine + interruptible) is an optional later layer | Can't pre-author a curriculum for arbitrary software; structure only fits subjects with a spine (e.g. a codebase) |
| Placement | `point_at` = **inline tool** (`src/browser-tools.ts`); overlay = **Swift menubar app** (extend existing `hudWindow`); Structured layer = a **skill** via `work` | Sutando CLAUDE.md decision guide; inline lane is the sub-second path, task-bridge is too slow for the hot loop |
| Invocation | **Hotkey-gated** for v1; pure-voice ambient = future scope | Preserves Clicky's deliberate "press to ask"; no false-trigger pointing; screen captured only on the gesture (privacy parity) |
| Pointer brain | **AX-first → `gemini-3-flash-preview` vision fallback** (native point format) | See ADR-0001 |
| Subscription/CLI | Claude-on-subscription (`:7846` proxy) reserved for **Lesson-Spine generation only**, not per-point | `claude -p` spawn latency + 2026-06-15 Agent-SDK-credit cap + always-on ToS grey-zone make it wrong for the hot loop |

## 3. Architecture — Clicky → Sutando

| Clicky piece | Sutando home | Action |
|---|---|---|
| Push-to-talk | Always-on `bodhi` VoiceSession + a dedicated arming hotkey | Reuse; add hotkey gate |
| Screenshot | `:7845` capture server | Reuse |
| `[POINT]` brain | **new `point_at` inline tool** | AX-first (via `macos-use`) → `gemini-3-flash-preview` native-format fallback |
| TTS | Voice agent already speaks | Reuse |
| `OverlayWindow` flight | Extend `src/Sutando/main.swift` `hudWindow` (already `.screenSaver`, click-through, all-spaces) with a SwiftUI triangle + bezier flight | Port rendering only; drive via local IPC from `point_at` |
| Lesson Spine (Structured) | New skill; analysis reuses `codebase-to-course` | Later layer |

## 4. Empirical evidence (POCs run during the grill)

- **AX POC** (`/tmp/ax-poc.swift`): AX returns exact frames, zero cost, no network — **proven for native apps** (Finder 45 framed nodes). But full-tree walk is **213–540 ms** (not "ms"), and **VSCode/Electron is near-blind** (228 nodes, 6 framed; `AXManualAccessibility` unlock did not rescue it). ⇒ vision is the *primary* path for marquee targets.
- **Vision POC** (`/tmp/gemini_point_poc.py` vs VSCode): `gemini-3.1-flash-lite` (current model) **108 px off, ~14 s** — unfit. `gemini-3.1-pro-preview` → 429 on the free key (needs billing). **`gemini-3-flash-preview`, thinking off, native `[y,x]` 0–1000 format → CLAUDE.md 3 px, Search icon 1 px on the free key.** Clicky's raw-pixel prompt on the same model: 23–69 px off → **use Gemini's native format, not Clicky's prompt.**

## 5. Open items (not blocking the design; tracked)

1. **Latency stabilisation** — free-tier Gemini variance (~1.5 s good, 7–8 s spikes, ~6-call RPM). Levers: image-downscale size vs accuracy, response caching, paid key floor, parallel AX+vision race.
2. **Coordinate transforms** — AX (top-left global CG points) and Gemini (normalized 0–1000 of the sent image) each need their own transform into the overlay's AppKit (bottom-left) space.
3. **AX tree → element selector** — the fast path still needs a cheap label/role match over the `macos-use` tree to turn intent into a Target (the half the AX POC did not cover).
4. **IPC** — how `point_at` drives the Swift overlay (the menubar app currently watches files / `~/.config`; choose socket vs localhost endpoint vs command file).
5. **Embodiment/persona** — replicate Clicky's blue triangle vs a Sutando-native pointer (reversible; defer).
6. **`POINTER_MODEL` preview-id longevity** — `point_at` uses `gemini-3-flash-preview`. The May 25 2026 `gemini-3.1-flash-lite-preview` deprecation (#715) is a *different* model family and does **not** affect this one — verified on [Google's deprecation page](https://ai.google.dev/gemini-api/docs/deprecations) on 2026-05-19: no shutdown date announced. Escape hatch is the `POINTER_MODEL` env override (zero code change). Tracked follow-up: re-run the pointing POC on the GA id if/when one is published before swapping the default.

## 6. v1 — productionized into Sutando ✅

The tracer (`pointer-teacher-tracer/`) proved the whole embodied loop. It is now folded into the live app on branch `feat/pointer-teacher-design`:

| Tracer slice | Production home | Status |
|---|---|---|
| `resolver.py` (capture → `gemini-3-flash-preview` native format, thinking off) | **`point_at` inline tool** — `src/browser-tools.ts` | ✅ done |
| `pointer-overlay.swift` (screenSaver click-through window + bezier flight) | **`PointerOverlayView` + `setupPointerOverlay/flyPointer/holdPointer`** — `src/Sutando/main.swift` | ✅ done |
| `/tmp/pointer-cmd.json` file poll | **`<workspace>/state/pointer-cmd.json`**, watched with the app's existing `DispatchSource` dir-watch idiom (same as `watchResults()`) — resolves open item #4 | ✅ done |
| `say` via `say(1)` | Voice agent narrates — `point_at` returns `say` + an instruction forcing Gemini to speak it | ✅ done |
| Standalone binary launched from agent shell (could not reach the GUI session) | Runs inside `Sutando.app`'s real menubar GUI session — **this is the fix for the "I didn't see any pointer" visibility problem** | ✅ done |

End-to-end: voice/utterance → `point_at(query)` → capture `:7845` → `gemini-3-flash-preview` → `state/pointer-cmd.json` → `Sutando.app` flies the triangle → voice speaks the `say` line.

**Invocation (v1 simplification).** The decision log called for a hotkey gate. In practice the always-on `bodhi` voice agent already provides the gesture, and the privacy property the hotkey was protecting ("screen captured only on the gesture") already holds — `point_at` captures *only* when invoked, never ambiently. So v1 ships **voice-driven** (the tool description triggers it on "where do I…/show me…/point at…/teach me…"); a dedicated arming hotkey stays future scope, not a blocker. Not an ADR — fully reversible (add a `point_teacher` action to `registerHotKey()` later).

**Still open (tracked, not blocking):** AX-first resolution ahead of the vision call (open item #3) — the tracer and this v1 are deliberately vision-only, the path the POC proved. Latency stabilisation (open item #1). Multi-display (single-display scope guard holds). Lesson Spine / knowledge journal (Structured-teaching layer, later).
