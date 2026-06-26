---
name: zoom
description: "Zoom meeting control for Sutando's voice and phone agents — contributes the summon (join + screen share), dismiss (leave meeting), and join_zoom (join with computer audio, no share) inline tools."
when_to_use: "Loaded automatically at agent startup as a manifest skill — its tools fire when the user says 'summon', 'share my screen', 'start zoom', 'join the zoom', 'dismiss', 'leave zoom', etc. Not directly slash-invoked."
---

# Zoom

Manifest-loaded skill that contributes three Zoom inline tools into the agent's runtime tool table. Picked up at startup by `loadSkillManifestTools()` in `src/inline-tools.ts` and merged into `inlineTools` + `ownerOnlyTools`, so the tools reach both the web voice agent and the phone agent for owner callers.

## Tools

- **`summon`** — opens Zoom (via the `zoommtg://` deeplink), joins the meeting, retries the Join click, and starts screen sharing so the user can see and control the Mac remotely. Optionally dials in via the phone server for voice. Use for "summon", "share my screen", "start zoom", "let me see your screen".
- **`dismiss`** — leaves the current Zoom meeting (stops screen share, opens the leave dialog, confirms; force-kills Zoom if windows linger). Use for "dismiss", "leave zoom", "end meeting", "hang up zoom".
- **`join_zoom`** — joins a Zoom meeting via the desktop app with computer audio, no screen share. Use for "join the zoom", "join meeting", or when the user provides a meeting ID.

## Configuration

- **`ZOOM_DEFAULT_SHARE_SCREEN`** (non-secret, in `manifest.json` `config`) — `"true"` by default. Set to `"false"` in the host environment to make `summon` skip screen sharing unless the caller explicitly passes `shareScreen: true`.
- **`ZOOM_PERSONAL_MEETING_ID`**, **`ZOOM_PERSONAL_PASSCODE`** (secrets — host `.env` only, never in the manifest) — the personal-room defaults used when `summon` / `join_zoom` are called without an explicit meeting ID.

## Notes

- Extracted from `src/meeting-tools.ts` in issue #786.
- Converted from a static hard-import into core to a manifest-loaded skill in issue #976, so core no longer has a compile-time dependency on `skills/zoom/tools.ts` and the skill is genuinely optional.
- an external plugin (#1427) intentionally overrides `dismiss` with its own implementation (SIGTERM self instead of Zoom AppleScript); its dedupe-by-name loop keeps the override and drops this skill's `dismiss`.
