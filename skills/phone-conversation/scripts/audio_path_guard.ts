// Path allowlist for `/play-audio` and related ffmpeg-input paths in
// `conversation-server.ts`.
//
// Background: `/play-audio` accepts a `body.path` over HTTP POST and
// passes it to `spawn('ffmpeg', ['-i', body.path, ...])`. The pre-fix
// handler validated only `existsSync(body.path)` — no allowlist — and
// the phone server binds to `0.0.0.0` (LAN-reachable to accommodate
// ngrok's local-tunnel client). Net: any caller on the local network
// could request that ffmpeg open any file the server's user can read,
// and the resulting PCM stream would be sent to the active call's
// Twilio WebSocket — i.e., to whoever happens to be on the phone with
// the owner at that moment. ffmpeg also resolves URLs in the `-i` slot,
// which existsSync happens to block (existsSync("http://...") is
// false), but a symlink under an allowed-looking prefix pointing at
// any local file would bypass the check.
//
// This module owns the allowlist. Used by `/play-audio` and the
// QuickTime-resume re-stream path (the second `ffmpeg` spawn in
// `conversation-server.ts`).

import { existsSync, realpathSync } from 'node:fs';

// Audio/video files live in `/tmp/sutando-*` per the recording skill's
// convention (QuickTime saves to /tmp/sutando-recording-<ts>.mov; the
// narrator pipeline writes /tmp/sutando-recording-*-narrated*.mov).
// `/private/tmp/...` is the macOS realpath of `/tmp/...` — both are
// listed so a `realpath`-resolved value matches.
//
// Keep this set TIGHT. New legitimate sources (e.g., a future
// recordings dir under the workspace) should be added explicitly with
// a justifying comment.
export const AUDIO_ALLOWED_PREFIXES: readonly string[] = [
	'/tmp/sutando-',
	'/private/tmp/sutando-',
];

/**
 * True iff `fpath` is a regular file AND its real path (symlinks +
 * `..` segments resolved) starts with one of the allowed prefixes.
 *
 * Mirrors the `_is_path_sendable` shape used by `discord-bridge.py`
 * and `telegram-bridge.py` for `[file: /path]` marker delivery (the
 * established codebase pattern for path-allowlist gating).
 */
export function isAllowedAudioPath(fpath: string): boolean {
	if (!fpath || typeof fpath !== 'string') return false;
	if (!existsSync(fpath)) return false;
	let real: string;
	try {
		real = realpathSync(fpath);
	} catch {
		return false;
	}
	for (const prefix of AUDIO_ALLOWED_PREFIXES) {
		if (real === prefix.slice(0, -1) || real.startsWith(prefix)) {
			return true;
		}
	}
	return false;
}
