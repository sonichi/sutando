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

	it('reports the URL it actually opened', () => {
		assert.match(SRC, /return \{ status: reused \? 'reused' : 'opened', url: target \}/);
	});
});
