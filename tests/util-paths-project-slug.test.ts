import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Tests for util_paths.claudeProjectSlug() (core-memory slug bug).
 * Mirrors tests/util-paths-project-slug.test.py.
 *
 * Every project-slug consumer used to re-implement the "dash every
 * non-alphanumeric character" regex inline, and drifted: health-check.py,
 * voice-agent.ts, and voice-context.ts all only replaced "/" — silently
 * resolving to a nonexistent projects/<slug>/ dir on any install path
 * containing a space or dot (e.g. a desktop-bundled checkout under
 * "Application Support/space.ag2.app/"). This locks the ONE correct
 * derivation behind a single shared helper so it can't drift again.
 */

import { claudeProjectSlug } from '../src/util_paths.js';

describe('claudeProjectSlug (core-memory slug bug)', () => {
	it('plain path matches the old slash-only baseline', () => {
		assert.equal(claudeProjectSlug('/Users/foo/bar'), '-Users-foo-bar');
	});

	it('dashes a space', () => {
		assert.equal(
			claudeProjectSlug('/Users/foo/My Documents/bar'),
			'-Users-foo-My-Documents-bar',
		);
	});

	it('dashes a dot', () => {
		assert.equal(
			claudeProjectSlug('/Users/foo/space.ag2.app/bar'),
			'-Users-foo-space-ag2-app-bar',
		);
	});

	it('matches the desktop-bundled engine path that triggered the report', () => {
		const bundled = '/Users/u/Library/Application Support/space.ag2.app/engine/sutando';
		assert.equal(
			claudeProjectSlug(bundled),
			'-Users-u-Library-Application-Support-space-ag2-app-engine-sutando',
		);
	});

	it('preserves alphanumerics', () => {
		assert.equal(claudeProjectSlug('abc123'), 'abc123');
	});
});
