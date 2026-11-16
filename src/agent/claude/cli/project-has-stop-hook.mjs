#!/usr/bin/env node
// Exit 0 when a settings file's hooks.Stop runs `bash <script>` for the same file as <script>, else 1.
// Usage: node project-has-stop-hook.mjs <script> <settings.json>...
import fs from 'node:fs';

const real = (p) => {
	try {
		return fs.realpathSync(p);
	} catch {
		return null;
	}
};

// One shell word as install-claude-hooks.sh writes it: '…' with '\'' escapes, "…", or bare.
const unquote = (w) =>
	/^'.*'$/s.test(w) ? w.slice(1, -1).replace(/'\\''/g, "'") : /^".*"$/s.test(w) ? w.slice(1, -1) : w;

const [script, ...files] = process.argv.slice(2);
const want = script ? real(script) : null;
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
			if (m && real(unquote(m[1].trim())) === want) found = true;
		}
	}
}
process.exit(found ? 0 : 1);
