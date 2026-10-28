/** The click tool's shortcut becomes a fixed System Events script:
 *  only known modifiers/keys are accepted, text keys stay string data. */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { shortcutScript, appleScriptString } from '../src/keyboard-shortcut.js';

const script = (s: string) => {
	const r = shortcutScript(s);
	assert.ok('script' in r, `expected a script for ${JSON.stringify(s)}: ${JSON.stringify(r)}`);
	return r.script;
};

describe('shortcutScript', () => {
	it('keeps the existing character shortcuts', () => {
		assert.equal(script('cmd+shift+5'), 'tell application "System Events" to keystroke "5" using {command down, shift down}');
		assert.equal(script('a'), 'tell application "System Events" to keystroke "a"');
		assert.equal(script('Ctrl+Option+T'), 'tell application "System Events" to keystroke "t" using {control down, option down}');
	});

	it('maps named keys to key codes', () => {
		assert.equal(script('cmd+return'), 'tell application "System Events" to key code 36 using {command down}');
		assert.equal(script('escape'), 'tell application "System Events" to key code 53');
		assert.equal(script('cmd+tab'), 'tell application "System Events" to key code 48 using {command down}');
	});

	it('accepts the plus and minus keys', () => {
		assert.equal(script('cmd++'), 'tell application "System Events" to keystroke "+" using {command down}');
		assert.equal(script('cmd+-'), 'tell application "System Events" to keystroke "-" using {command down}');
	});

	it('quotes a quote or backslash key as string data', () => {
		assert.equal(script('cmd+"'), 'tell application "System Events" to keystroke "\\"" using {command down}');
		assert.equal(script('\\'), 'tell application "System Events" to keystroke "\\\\"');
	});

	it('rejects a multi-character key that is not a named key', () => {
		const r = shortcutScript('cmd+return & (do shell script "touch /tmp/sutando-probe")');
		assert.ok('error' in r);
		assert.match(r.error, /unsupported key/);
	});

	it('rejects an unknown modifier instead of dropping it', () => {
		const r = shortcutScript('hyper+a');
		assert.ok('error' in r);
		assert.match(r.error, /unsupported modifier "hyper"/);
	});

	it('rejects an empty or control-character key', () => {
		assert.ok('error' in shortcutScript('cmd+'));
		assert.ok('error' in shortcutScript('cmd+\n'));
	});

	it('appleScriptString escapes backslash before quote', () => {
		assert.equal(appleScriptString('a\\"b'), '"a\\\\\\"b"');
	});
});


