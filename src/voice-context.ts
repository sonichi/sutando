/**
 * Builds a system prompt for the Claude Code subprocess that injects
 * Sutando identity and user context from the memory system.
 */

import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolveWorkspace } from './workspace_default.js';
import { claudeHomePath, claudeProjectSlug } from './util_paths.js';

function defaultMemoryDir(): string {
    const repo = resolve(join(import.meta.dirname, '..'));
    const slug = claudeProjectSlug(repo);
    return claudeHomePath('projects', slug, 'memory');
}

const MEMORY_DIR = process.env.SUTANDO_MEMORY_DIR || defaultMemoryDir();
// fileURLToPath, never `.pathname`: URL.pathname stays percent-encoded, so on
// the desktop-bundled install ("~/Library/Application Support/…") REPO_DIR
// became ".../Application%20Support/..." and every join() below silently
// pointed at a path that does not exist — no error, just an empty transcript
// and missing CLAUDE.md context.
const REPO_DIR = fileURLToPath(new URL('..', import.meta.url)).replace(/\/$/, '');
const WORKSPACE_DIR = resolveWorkspace();

function readMemory(filename: string): string | null {
	const path = join(MEMORY_DIR, filename);
	if (!existsSync(path)) return null;
	try {
		// Strip YAML frontmatter
		return readFileSync(path, 'utf-8').replace(/^---[\s\S]*?---\s*\n/, '').trim();
	} catch {
		return null;
	}
}

/**
 * Build a concise context summary for the Gemini voice agent.
 * Gives Gemini awareness of the current system state and user context.
 */
export function buildVoiceAgentContext(): string {
	const userProfile = readMemory('user_profile.md');
	const lines: string[] = [];

	if (userProfile) {
		lines.push('USER CONTEXT:', userProfile.slice(0, 500), '');
	}

	// Read build log summary
	const buildLog = join(WORKSPACE_DIR, 'build_log.md');
	if (existsSync(buildLog)) {
		try {
			const content = readFileSync(buildLog, 'utf-8');
			const scoreMatch = content.match(/\*\*Score: (.+?)\*\*/);
			if (scoreMatch) {
				lines.push(`SYSTEM STATUS: ${scoreMatch[1]}`, '');
			}
		} catch { /* best effort */ }
	}

	// build_log.md is append-at-bottom: the newest header is the LAST match, and
	// items must be scoped to it — unscoped, slice(5) returns the file preamble.
	if (existsSync(buildLog)) {
		try {
			const content = readFileSync(buildLog, 'utf-8');
			const headers = [...content.matchAll(/## \d{4}-\d{2}-\d{2} — .+/g)];
			const newest = headers.length ? headers[headers.length - 1] : null;
			if (newest) {
				const from = (newest.index ?? 0) + newest[0].length;
				const rest = content.slice(from);
				const next = rest.search(/## \d{4}-\d{2}-\d{2} — /);
				const section = next === -1 ? rest : rest.slice(0, next);
				// A section with no bulleted items renders header-only ON PURPOSE:
				// borrowing items from elsewhere is the mispairing this fixes.
				const items = section.match(/^- \*\*.+?\*\*.*/gm) ?? [];
				lines.push('RECENT ACTIVITY:', newest[0].replace('## ', '  '), ...items.slice(0, 5).map(i => '  ' + i), '');
			}
		} catch { /* best effort */ }
	}

	// Read recent phone call summaries (last 3 calls)
	const callsFile = join(REPO_DIR, 'results', 'calls', 'calls.jsonl');
	if (existsSync(callsFile)) {
		try {
			const callLines = readFileSync(callsFile, 'utf-8').trim().split('\n').filter(Boolean);
			const recentCalls = callLines.slice(-3).reverse();
			if (recentCalls.length > 0) {
				lines.push('RECENT PHONE CALLS:');
				for (const line of recentCalls) {
					try {
						const call = JSON.parse(line);
						const who = call.caller || call.to || 'unknown';
						const when = call.start_time || call.timestamp || '';
						const summary = call.summary || call.topic || '(no summary)';
						const dateStr = when ? new Date(when).toLocaleDateString() : '';
						lines.push(`  ${dateStr} ${who}: ${summary.slice(0, 120)}`);
					} catch { /* skip malformed */ }
				}
				lines.push('');
			}
		} catch { /* best effort */ }
	}

	return lines.join('\n');
}

export function buildSutandoSystemPrompt(): string {
	const userProfile = readMemory('user_profile.md');
	const responseStyle = readMemory('feedback_response_style.md');
	const minimalCost = readMemory('feedback_minimal_cost_max_value.md');

	const lines: string[] = [
		'You are Sutando\'s task execution engine, acting on behalf of the user.',
		'Handle anything delegated: research, writing, email, scheduling, code,',
		'financial tasks, web browsing, file management, content creation.',
		'',
		'Rules:',
		'- For irreversible actions (sending email, deleting files, financial transactions),',
		'  confirm with the user unless standing approval has been given.',
		'- Complete tasks the way the user would — match their voice and preferences.',
		'- Be concise in results; they are surfaced via voice.',
		'',
	];

	if (userProfile) {
		lines.push('## User context', userProfile, '');
	}

	if (responseStyle) {
		lines.push('## Communication style', responseStyle, '');
	}

	if (minimalCost) {
		lines.push('## Working style', minimalCost, '');
	}

	// Inject built-in capabilities so the subprocess knows what tools are available
	const claudeMd = join(REPO_DIR, 'CLAUDE.md');
	if (existsSync(claudeMd)) {
		try {
			const content = readFileSync(claudeMd, 'utf-8');
			// Extract the "Built-in capabilities" section
			const capMatch = content.match(/## Built-in capabilities\n([\s\S]*?)(?=\n## |\n$)/);
			if (capMatch) {
				lines.push('## Available tools and capabilities', capMatch[1].trim(), '');
			}
		} catch { /* best effort */ }
	}

	lines.push(
		'## Memory',
		`User memory files: ${MEMORY_DIR}`,
		'Read relevant files when user preferences or history would improve task quality.',
	);

	return lines.join('\n');
}
