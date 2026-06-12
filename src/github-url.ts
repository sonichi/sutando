// Pure GitHub-target resolution for the discord-voice `open_github_url` tool
// (#1427 demo). Kept pure + exported so it's unit-testable (the side effects —
// `open` + `gh` — live in the tool's execute()). This is the layer that fixes
// the "opens the private mirror" bug: the canonical repo is fixed here, never
// inferred from a git remote.

/** Canonical Sutando repo, env-overridable. NEVER a per-instance private mirror. */
export const CANONICAL_REPO = process.env.SUTANDO_GH_REPO || 'sonichi/sutando';

export type GithubKind = 'open_repo' | 'open_pr' | 'open_issue' | 'recent_prs' | 'issues_by';

export interface GithubTarget {
	/** A browser URL to open (open_repo / open_pr / open_issue). */
	url?: string;
	/** Argument vector for a read-only `gh` query (recent_prs / issues_by). */
	ghArgs?: string[];
	/** Set when the request is malformed (missing n / who, unknown kind). */
	error?: string;
}

/**
 * Resolve a natural GitHub request to a concrete URL or a `gh` arg-vector,
 * always against the canonical repo. Pure: no I/O. `who` is sanitized to a
 * GitHub-username charset so it can be passed to execFileSync safely.
 */
export function resolveGithubTarget(
	kind: GithubKind,
	opts: { n?: number; who?: string; limit?: number; repo?: string } = {},
): GithubTarget {
	const repo = opts.repo || CANONICAL_REPO;
	const base = `https://github.com/${repo}`;
	switch (kind) {
		case 'open_repo':
			return { url: base };
		case 'open_pr':
			return opts.n ? { url: `${base}/pull/${opts.n}` } : { error: 'PR number required' };
		case 'open_issue':
			return opts.n ? { url: `${base}/issues/${opts.n}` } : { error: 'issue number required' };
		case 'recent_prs':
			return { ghArgs: ['pr', 'list', '--repo', repo, '--limit', String(opts.limit || 5), '--json', 'number,title,author'] };
		case 'issues_by': {
			if (!opts.who) return { error: 'username required' };
			const safe = opts.who.replace(/[^A-Za-z0-9_-]/g, '');
			if (!safe) return { error: 'invalid username' };
			return { ghArgs: ['issue', 'list', '--repo', repo, '--author', safe, '--limit', '10', '--json', 'number,title'] };
		}
		default:
			return { error: 'unknown kind' };
	}
}
