/**
 * Where the durable X login lives — resolution only, no side effects.
 *
 * Separate module for the same reason as composer-text.mjs and profile-match.mjs:
 * `x-post-browser.mjs` does work at import (argv parsing, mkdir, Chrome resolution),
 * so it cannot be imported by a test, and a test that re-implemented this precedence
 * would stay green while the shipped resolution drifted (#1414).
 *
 * Dependencies are injected rather than imported so the precedence can be exercised
 * without a real filesystem or a real workspace resolver.
 */

/**
 * Resolve the profile directory.
 *
 * Precedence — the manifest convention (CLI/env > manifest > derived):
 *   1. `X_BROWSER_PROFILE`, declared in skills/x-twitter/manifest.json so an operator
 *      override is a documented setting rather than an ad-hoc env var (qingyun, #2133).
 *   2. `<workspace>/data/x-browser-profile`, via the canonical workspace resolver.
 *      `data/` is per-user mutable state under the workspace contract AND sits in the
 *      vault `exclude` list — a browser profile carries live session cookies and must
 *      never be synced anywhere.
 *   3. The pre-#2133 location, ONLY while it exists and the canonical one does not.
 *      Moving the default silently would orphan a working X session and force a fresh
 *      sign-in, which is the one genuinely expensive step in this skill. Same
 *      reader-fallback-with-notice shape the workspace migration uses.
 *
 * @returns {{dir: string, source: 'env'|'workspace'|'legacy'|'unresolved', notice?: string}}
 */
export function resolveProfileDir({ env, workspace, legacyDir, exists }) {
	if (env) return { dir: env, source: 'env' };
	if (!workspace) {
		return {
			dir: legacyDir,
			source: 'unresolved',
			notice:
				'could not resolve the workspace; set X_BROWSER_PROFILE to choose a profile dir.',
		};
	}
	const canonical = `${workspace}/data/x-browser-profile`;
	if (!exists(canonical) && exists(legacyDir)) {
		return {
			dir: legacyDir,
			source: 'legacy',
			notice:
				`using the pre-#2133 profile at ${legacyDir}. Move it to ${canonical} ` +
				'(or set X_BROWSER_PROFILE) — this fallback is temporary.',
		};
	}
	return { dir: canonical, source: 'workspace' };
}
