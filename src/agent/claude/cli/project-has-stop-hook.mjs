#!/usr/bin/env node
// Exit 0 when a settings file's hooks.Stop runs `bash <script>` for the same file as <script>, else 1.
// Usage: node project-has-stop-hook.mjs <session cwd> <script> <settings.json>...
import fs from 'node:fs';
import path from 'node:path';

const real = (p) => {
	try {
		return fs.realpathSync(p);
	} catch {
		return null;
	}
};

// One shell word as install-claude-hooks.sh writes it: '…' with '\'' escapes, "…", or a bare word.
// A bare word with whitespace is several words to bash, so it names no single script.
const unquote = (w) => {
	if (/^'.*'$/s.test(w)) return w.slice(1, -1).replace(/'\\''/g, "'");
	if (/^".*"$/s.test(w)) return w.slice(1, -1);
	return /\s/.test(w) ? null : w;
};

const [cwd, script, ...files] = process.argv.slice(2);
const want = cwd && script ? real(script) : null;
let found = false;
for (const file of want ? files : []) {
	let settings;
	try {
		settings = JSON.parse(fs.readFileSync(file, 'utf8'));
	} catch {
		continue;
	}
	const blocks = settings?.hooks?.Stop;
	for (const block of Array.isArray(blocks) ? blocks : []) {
		for (const hook of Array.isArray(block?.hooks) ? block.hooks : []) {
			if (hook?.type !== 'command' || typeof hook.command !== 'string') continue;
			const m = hook.command.trim().match(/^bash\s+(.+)$/s);
			const target = m ? unquote(m[1].trim()) : null;
			// Claude Code runs a hook in the session's cwd, so a relative script resolves there.
			if (target && real(path.resolve(cwd, target)) === want) found = true;
		}
	}
}
process.exit(found ? 0 : 1);
