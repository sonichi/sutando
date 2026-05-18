# Pointer Teacher brain: accessibility-first, Gemini-vision fallback

**Status:** accepted

The Pointer Teacher must resolve "the element the user means" to an exact on-screen point — fast, cheap, across arbitrary apps. We decided: query the macOS Accessibility (AXUIElement) tree first (exact element frames, ~hundreds of ms, zero cost, no network, no ToS risk) and fall back to a vision model only when the AX tree is empty or sparse. The vision model is `gemini-3-flash-preview` (non-lite) called with **Gemini's native normalized `[{"point":[y,x]}]` 0–1000 format and thinking disabled** — *not* Clicky's `[POINT:x,y]` raw-pixel prompt.

## Considered Options

- **Pure vision, Clicky parity** (every point via vision, Clicky's `[POINT:x,y]` prompt). Rejected: inherits Clicky's imprecision, per-point latency+cost on everything, and empirically Clicky's raw-pixel prompt is 23–69 px off on Gemini vs **1–3 px** with the native format.
- **AX only.** Rejected: Electron (VSCode) and GPU-canvas apps (DaVinci, Figma) expose little/no AX tree — exactly the highest-value teaching targets, which AX-only could not teach.
- **Gemini 3.1 Pro / billed key.** Best benchmark accuracy but requires enabling Gemini billing; `gemini-3-flash-preview` on the existing *free* key already hit 1–3 px on the hard case, so billing is unjustified for v1.
- **Claude vision via the existing `:7846` subscription proxy.** Clicky's actual model, already wired at no API cost — but the claude-agent-sdk/CLI path adds spawn latency and is subject to the 2026-06-15 Agent-SDK-credit cap. Kept for non-hot-loop Lesson-Spine generation, not per-point resolution.
- **Local UI-TARS / MolmoPoint on the Mac Studio node.** Zero cost/ToS, ~61% ScreenSpot-Pro — deferred as a future privacy/cost optimisation; too much infra for v1.

## Consequences

- The "teach me the codebase in VSCode" example runs on **vision, not AX** (VSCode is AX-blind: 228 nodes, 6 framed, `AXManualAccessibility` unlock did not help). Vision is the *primary* path for marquee targets; AX is the fast/free path for native apps, menus, system UI.
- Two coordinate sources feed one overlay — AX (top-left global CG points) and Gemini (normalized 0–1000 of the sent image) — each needing its own transform into the overlay's AppKit space. Known correctness task.
- Free-tier Gemini latency is variable (~1.5 s good, 7–8 s spikes, ~6-call RPM). Tolerable only because invocation is hotkey-gated (not ambient); stabilising it (paid key / caching / image-size tuning) is a tracked follow-up.
- The Pointer Teacher must NOT reuse Sutando's existing `gemini-3.1-flash-lite` vision model — proven unfit for pointing (108 px off, ~14 s).
