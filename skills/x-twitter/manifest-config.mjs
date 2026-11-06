/**
 * Read settings DECLARED in this skill's manifest.json — resolution only, no side effects.
 *
 * manifest.json grew a `config` block for the three settings this skill reads, but
 * nothing ever consulted it: every mention of "manifest.json" in the skill was a
 * COMMENT, and each call site was still `process.env.X || '<literal>'`. So the block
 * satisfied the letter of the convention while an operator editing a declared value
 * got silence. It is invisible today only because the declared values happen to equal
 * the hardcoded literals — the moment they diverge, the manifest is the one that loses.
 *
 * skills/MANIFEST.md is explicit about why this matters HERE specifically: the loader
 * exports `config` into `process.env` at voice-agent startup, but this script runs as
 * its own `node` process (SKILL.md invokes it from a shell, and so do cron and the
 * agent's Bash tool). Outside that parent process it inherits nothing, and the
 * documented order for exactly that case is:
 *
 *     CLI arg  >  env override  >  manifest.json config[key]  >  built-in default
 *
 * "Read the manifest directly when needed. Never wire a bare invented os.environ[...]
 * as the *primary* source."
 *
 * Deps are injected for the same reason as profile-dir.mjs: the precedence is the part
 * with the bugs in it, and it should be exercisable without a real filesystem.
 */

/**
 * Parse the `config` block out of a manifest. NEVER throws.
 *
 * A missing or malformed manifest must not be able to break posting — the built-in
 * defaults still run the skill, so failing hard here would trade a working publish
 * path for a config file typo.
 *
 * @returns {Record<string, unknown>} the config block, or {} if unreadable.
 */
export function readManifestConfig({ manifestPath, readFile }) {
	try {
		const parsed = JSON.parse(readFile(manifestPath, 'utf8'));
		const config = parsed && parsed.config;
		return config && typeof config === 'object' ? config : {};
	} catch {
		return {};
	}
}

/**
 * Resolve one declared setting: env override > manifest config > built-in default.
 *
 * An EMPTY value counts as unset at every rung. That is not cosmetic: X_BROWSER_PROFILE
 * ships as `""` precisely so it falls through to the derived per-workspace path, and an
 * empty env var (`X_BROWSER_PROFILE= node ...`) means "unset", not "the root directory".
 *
 * @returns {{value: string, source: 'env'|'manifest'|'default'}}
 */
export function resolveSetting(key, { env, config, fallback }) {
	const fromEnv = env ? env[key] : undefined;
	if (fromEnv !== undefined && fromEnv !== '') return { value: String(fromEnv), source: 'env' };

	const fromManifest = config ? config[key] : undefined;
	if (fromManifest !== undefined && fromManifest !== '' && fromManifest !== null) {
		return { value: String(fromManifest), source: 'manifest' };
	}
	return { value: fallback, source: 'default' };
}
