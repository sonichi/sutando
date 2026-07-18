#!/usr/bin/env node
// Build the Claude Code `--settings` JSON for the Sutando core session.
//
// Always registers the AskUserQuestion safety guard (a PreToolUse `deny` hook —
// the headless core has no interactive user, so an AskUserQuestion tool call
// would block the session forever; see hooks/skip-ask-user-question.py). When
// obs capture is enabled, the collector's hook settings are MERGED into the same
// JSON so a single `--settings` flag carries both (multiple `--settings` flags
// are undocumented / last-wins, so we never rely on passing more than one).
//
// Composition — not obs internals — lives here on purpose: the obs event map is
// owned solely by src/observability/claude/hooks/build-hook-settings.mjs; this
// builder treats the obs settings as an opaque JSON blob and array-concats it
// with the guard, so the two concerns never drift.
//
// Usage:  node build-core-settings.mjs <abs-path-to-guard-hook.py> [<obs-settings-json>]
//   arg1 (required): path to the guard hook script (skip-ask-user-question.py).
//   arg2 (optional): the obs `--settings` JSON string from build-hook-settings.mjs;
//                    empty / omitted → obs hooks are not included.
// Prints the merged settings JSON to stdout (exit 2 on a missing guard path,
// exit 3 on an unparseable obs-settings blob).

const guardHook = process.argv[2];
if (!guardHook) {
	process.stderr.write('usage: build-core-settings.mjs <guard-hook-path> [<obs-settings-json>]\n');
	process.exit(2);
}
const obsJson = process.argv[3] || '';

// POSIX single-quote a string so it survives as one shell word regardless of
// spaces, $, backticks, or quotes: wrap in '…' and replace each embedded ' with
// the '\'' idiom. (Same guarantee as build-hook-settings.mjs — the checkout path
// can contain any of these.)
const shq = (s) => "'" + s.replace(/'/g, "'\\''") + "'";

// The guard is a PreToolUse hook scoped to the single `AskUserQuestion` tool
// (exact-string matcher). Defense-in-depth: the script itself re-checks the tool
// name and no-ops for anything else, so the matcher is belt-and-suspenders.
const guardCommand = `python3 ${shq(guardHook)}`;
const guardSettings = {
	hooks: {
		PreToolUse: [{ matcher: 'AskUserQuestion', hooks: [{ type: 'command', command: guardCommand }] }],
	},
};

// Deep-merge hook settings by CONCATENATING the per-event arrays (Claude Code
// runs every registered hook for an event). Plain object spread / `*`-style
// merges would REPLACE one PreToolUse array with the other — dropping either the
// guard or the obs collector. Order: guard first so its deny is evaluated, then
// obs (which still records the tool.call for AskUserQuestion — both hooks fire).
function mergeHookSettings(...sources) {
	const out = { hooks: {} };
	for (const src of sources) {
		const hooks = (src && src.hooks) || {};
		for (const [event, entries] of Object.entries(hooks)) {
			out.hooks[event] = (out.hooks[event] || []).concat(entries);
		}
	}
	return out;
}

let obsSettings = null;
if (obsJson.trim()) {
	try {
		obsSettings = JSON.parse(obsJson);
	} catch (e) {
		process.stderr.write(`build-core-settings: obs settings JSON is unparseable: ${e.message}\n`);
		process.exit(3);
	}
}

process.stdout.write(JSON.stringify(mergeHookSettings(guardSettings, obsSettings)));
