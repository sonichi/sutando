/**
 * open_url normalizes a missing scheme before it reaches AppleScript.
 *
 * Chrome's `make new tab with properties {URL:...}` rejects a bare host with
 * "Invalid URL entered. (5)" — only the omnibox infers a scheme. The tool's own
 * description advertises the bare-host form ("open github.com"), so the model
 * passes exactly the input that failed.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { withScheme } from '../src/url-scheme.js';

const SRC = readFileSync(
	join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'browser-tools.ts'),
	'utf8',
);

describe('withScheme', () => {
	it('prefixes https:// on the bare host the model actually passes', () => {
		assert.equal(withScheme('NBA.com'), 'https://NBA.com');
		assert.equal(withScheme('github.com'), 'https://github.com');
		assert.equal(withScheme('www.nba.com/scores'), 'https://www.nba.com/scores');
	});

	it('leaves a hierarchical scheme alone', () => {
		assert.equal(withScheme('https://nba.com'), 'https://nba.com');
		assert.equal(withScheme('http://localhost:3000'), 'http://localhost:3000');
		assert.equal(withScheme('chrome://flags'), 'chrome://flags');
		assert.equal(withScheme('file:///tmp/x.html'), 'file:///tmp/x.html');
	});

	it('leaves an opaque scheme alone — about:blank is called out in the source', () => {
		assert.equal(withScheme('about:blank'), 'about:blank');
		assert.equal(withScheme('mailto:a@b.com'), 'mailto:a@b.com');
		assert.equal(withScheme('data:text/plain,hi'), 'data:text/plain,hi');
	});

	it('treats host:port as a host, not a scheme — the case a naive /^\\w+:/ gets wrong', () => {
		// "localhost:" matches a scheme pattern, but what follows is a port.
		assert.equal(withScheme('localhost:3000'), 'https://localhost:3000');
		assert.equal(withScheme('127.0.0.1:8080'), 'https://127.0.0.1:8080');
	});

	it('produces a string new URL() can parse, which the bare host could not', () => {
		assert.throws(() => new URL('NBA.com'));
		assert.equal(new URL(withScheme('NBA.com')).origin, 'https://nba.com');
	});

	// Refusing a digit after the colon also caught digit-content schemes,
	// mangling them into `https://tel:911`.
	it('leaves digit-opaque schemes alone (tel/sms/callto)', () => {
		assert.equal(withScheme('tel:911'), 'tel:911');
		assert.equal(withScheme('tel:5551234'), 'tel:5551234');
		assert.equal(withScheme('sms:5551234'), 'sms:5551234');
		assert.equal(withScheme('callto:5551234'), 'callto:5551234');
		assert.equal(withScheme('TEL:911'), 'TEL:911', 'scheme match is case-insensitive');
	});

	it('still qualifies host:port, which is what the digit rule exists for', () => {
		assert.equal(withScheme('localhost:3000'), 'https://localhost:3000');
		assert.equal(withScheme('127.0.0.1:8080'), 'https://127.0.0.1:8080');
		assert.equal(withScheme('[::1]:8080'), 'https://[::1]:8080');
	});

	// `example.com:8080` is both a scheme and an authority and nothing decides
	// which, so it keeps its input form; an explicit scheme qualifies it.
	it('leaves an ambiguous dotted host:port alone rather than guessing', () => {
		assert.equal(withScheme('example.com:8080/x'), 'example.com:8080/x');
		assert.equal(withScheme('https://example.com:8080/x'), 'https://example.com:8080/x');
		assert.equal(withScheme('example.com/x'), 'https://example.com/x');
	});

	// A scheme allowlist can only preserve the schemes it lists. Any other
	// scheme whose content opens with a digit was rewritten as a web origin.
	it('preserves an unlisted scheme whose content opens with a digit', () => {
		assert.equal(withScheme('mailto:123@example.com'), 'mailto:123@example.com');
		assert.equal(withScheme('bitcoin:1A1zP1eP5Q'), 'bitcoin:1A1zP1eP5Q');
		assert.equal(withScheme('spotify:404'), 'spotify:404');
	});

	// Opposite direction: a reverse-DNS scheme is dotted, which is the ordinary
	// shape of an app deep link. Inferring host:port from "dotted" claimed it.
	it('preserves a dotted (reverse-DNS) scheme rather than reading it as host:port', () => {
		assert.equal(withScheme('com.example:123/path'), 'com.example:123/path');
		assert.equal(withScheme('com.spotify.music:456'), 'com.spotify.music:456');
		assert.equal(new URL(withScheme('com.example:123/path')).protocol, 'com.example:');
	});
});

describe('open_url wiring', () => {
	it('normalizes before building the AppleScript argument', () => {
		// The escape must consume the normalized value; escaping the raw input
		// would send the bare host to Chrome regardless of the helper.
		assert.match(SRC, /const target = withScheme\(url\);/);
		assert.match(SRC, /const safeUrl = target\.replace\(/);
	});

	it('parses the origin from the normalized value, so same-origin reuse still works', () => {
		// With the raw bare host, new URL() throws, targetOrigin stays '' and
		// every open silently became a new tab.
		assert.match(SRC, /targetOrigin = new URL\(target\)\.origin/);
	});
});
