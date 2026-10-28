/** Build a System Events script for a "cmd+shift+5"-style shortcut.
 *  Only known modifiers/keys are accepted, so input can't alter the script's structure. */

const MODIFIERS: Record<string, string> = {
	cmd: 'command down', command: 'command down',
	shift: 'shift down',
	ctrl: 'control down', control: 'control down',
	alt: 'option down', option: 'option down',
};

const NAMED_KEY_CODES: Record<string, number> = {
	enter: 36, return: 36, escape: 53, esc: 53, tab: 48, space: 49,
	delete: 51, backspace: 51, forwarddelete: 117,
	up: 126, down: 125, left: 123, right: 124,
	home: 115, end: 119, pageup: 116, pagedown: 121,
	f1: 122, f2: 120, f3: 99, f4: 118, f5: 96, f6: 97,
	f7: 98, f8: 100, f9: 101, f10: 109, f11: 103, f12: 111,
};

/** Quote a value as an AppleScript string literal. */
export function appleScriptString(value: string): string {
	return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

export type ShortcutScript = { script: string } | { error: string };

export function shortcutScript(shortcut: string): ShortcutScript {
	const lowered = shortcut.toLowerCase();
	let key: string;
	let modifierNames: string[];
	if (lowered === '+' || lowered.endsWith('++')) {
		key = '+';
		modifierNames = lowered.length > 1 ? lowered.slice(0, -2).split('+') : [];
	} else {
		modifierNames = lowered.split('+');
		const raw = modifierNames.pop() ?? '';
		key = raw.length === 1 ? raw : raw.trim();
	}
	const modifiers: string[] = [];
	for (const name of modifierNames) {
		const modifier = MODIFIERS[name.trim()];
		if (!modifier) return { error: `unsupported modifier "${name.trim()}" (use cmd, shift, ctrl or option)` };
		if (!modifiers.includes(modifier)) modifiers.push(modifier);
	}
	let action: string;
	const code = NAMED_KEY_CODES[key];
	if (code !== undefined) action = `key code ${code}`;
	else if (Array.from(key).length === 1 && !/[\u0000-\u001f\u007f]/.test(key)) action = `keystroke ${appleScriptString(key)}`;
	else return { error: `unsupported key "${key}" (use a single character or one of: ${Object.keys(NAMED_KEY_CODES).join(', ')})` };
	const using = modifiers.length ? ` using {${modifiers.join(', ')}}` : '';
	return { script: `tell application "System Events" to ${action}${using}` };
}
