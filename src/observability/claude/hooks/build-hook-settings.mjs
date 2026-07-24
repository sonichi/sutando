#!/usr/bin/env node
// Build the Claude Code `--settings` hooks JSON that registers the obs collector
// hook on every relevant lifecycle + tool event.
//
// Kept OUT of start-cli.sh's inline shell on purpose. Hand-rolled string
// interpolation broke two ways when the checkout path held special chars:
//   - a space split the command when Claude Code shelled it out
//   - a `"` or `\` broke the settings JSON outright
// and an inline `node <<'HEREDOC'` builder trips a bash 3.2 parser bug (a heredoc
// inside a double-quoted "$(...)" isn't treated as literal). So the builder lives
// here, where the path is (a) POSIX single-quoted inside the command — safe for
// spaces / $ / backtick — and (b) JSON-escaped in the payload by JSON.stringify.
//
// Usage:  node build-hook-settings.mjs <abs-path-to-obs-hook.sh>
// Prints the settings JSON to stdout (exit 2 on missing arg).
//
// The 10 event keys are all valid Claude Code hook events, verified against
// https://code.claude.com/docs/en/hooks.md (incl. UserPromptExpansion and
// MessageDisplay). Unknown keys would be silently ignored by the CLI.

import path from 'node:path';
import { fileURLToPath } from 'node:url';

const hookScript = process.argv[2];
if (!hookScript) {
	process.stderr.write('usage: build-hook-settings.mjs <hook-script-path>\n');
	process.exit(2);
}

// POSIX single-quote a string so it survives as one shell word regardless of
// spaces, $, backticks, or quotes: wrap in '…' and replace each embedded ' with
// the '\'' idiom.
const shq = (s) => "'" + s.replace(/'/g, "'\\''") + "'";

const command = `bash ${shq(hookScript)}`;
const hooks = [{ type: 'command', command }];
const life = [{ hooks }]; // lifecycle events (no matcher)
const tool = [{ matcher: '*', hooks }]; // tool events (matched)

// Skill-usage product telemetry (PostHog `feature_used{feature:"skill:<name>"}`).
// A SEPARATE pipeline from the obs collector above: the collector feeds local
// observability; this one broadens the anonymous product-telemetry feature
// surface from the two hand-instrumented scripts (morning-briefing, daily-
// insight) to EVERY skill the core runs, via a single PostToolUse[Skill] hook.
// Path is derived from this builder's own location (repo/src/observability/
// claude/hooks → repo/hooks) so it needs no new arg to start-cli.sh.
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..', '..', '..');
const skillTelemetryHook = path.join(repoRoot, 'hooks', 'skill-usage-telemetry.py');
const skillTelemetry = {
	matcher: 'Skill',
	hooks: [{ type: 'command', command: `python3 ${shq(skillTelemetryHook)}` }],
};

process.stdout.write(
	JSON.stringify({
		hooks: {
			UserPromptSubmit: life,
			UserPromptExpansion: life,
			MessageDisplay: life,
			PreToolUse: tool,
			// obs collector (all tools) + skill-usage product telemetry (Skill only)
			PostToolUse: [...tool, skillTelemetry],
			Stop: life,
			SessionStart: life,
			SessionEnd: life,
			PreCompact: life,
			Notification: life,
		},
	}),
);
