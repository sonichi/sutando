// Tests for the discord-voice open_github_url resolver (#1427).
// Covers the bug fix: "open PR N / the sutando repo" must resolve to the
// CANONICAL repo (sonichi/sutando), never a private mirror, and PR/issue
// numbers map deterministically to the right URL.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveGithubTarget, CANONICAL_REPO } from '../src/github-url.js';

test('canonical repo is sonichi/sutando by default (never a private mirror)', () => {
	assert.equal(CANONICAL_REPO, 'sonichi/sutando');
	assert.match(CANONICAL_REPO, /^sonichi\/sutando$/);
	assert.doesNotMatch(CANONICAL_REPO, /liususan091219|private|fork/);
});

test('open_repo → canonical repo URL', () => {
	assert.equal(resolveGithubTarget('open_repo').url, 'https://github.com/sonichi/sutando');
});

test('open_pr 1409 → sonichi/sutando/pull/1409 (the reported bug case)', () => {
	const t = resolveGithubTarget('open_pr', { n: 1409 });
	assert.equal(t.url, 'https://github.com/sonichi/sutando/pull/1409');
	assert.doesNotMatch(t.url!, /liususan091219/);
});

test('open_pr without a number → error, no URL', () => {
	const t = resolveGithubTarget('open_pr');
	assert.equal(t.url, undefined);
	assert.equal(t.error, 'PR number required');
});

test('open_issue N → issues/N', () => {
	assert.equal(resolveGithubTarget('open_issue', { n: 1008 }).url, 'https://github.com/sonichi/sutando/issues/1008');
});

test('recent_prs → gh pr list arg-vector against canonical repo', () => {
	const t = resolveGithubTarget('recent_prs', { limit: 5 });
	assert.deepEqual(t.ghArgs, ['pr', 'list', '--repo', 'sonichi/sutando', '--limit', '5', '--json', 'number,title,author']);
});

test('recent_prs default limit is 5', () => {
	assert.equal(resolveGithubTarget('recent_prs').ghArgs!.includes('5'), true);
});

test('issues_by → gh issue list --author, username sanitized (no shell metachars)', () => {
	const t = resolveGithubTarget('issues_by', { who: 'john-the-dev' });
	assert.deepEqual(t.ghArgs, ['issue', 'list', '--repo', 'sonichi/sutando', '--author', 'john-the-dev', '--limit', '10', '--json', 'number,title']);
});

test('issues_by sanitizes injection attempts in who', () => {
	const t = resolveGithubTarget('issues_by', { who: 'evil; rm -rf /' });
	// metachars + spaces stripped → "evilrm-rf" (safe for execFileSync)
	const authorIdx = t.ghArgs!.indexOf('--author') + 1;
	assert.doesNotMatch(t.ghArgs![authorIdx], /[;\s/]/);
});

test('issues_by without who → error', () => {
	assert.equal(resolveGithubTarget('issues_by').error, 'username required');
});

test('repo override is honored (env-overridable contract)', () => {
	assert.equal(resolveGithubTarget('open_pr', { n: 1, repo: 'sonichi/other' }).url, 'https://github.com/sonichi/other/pull/1');
});
