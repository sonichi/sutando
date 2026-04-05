/**
 * Builds a system prompt for the Claude Code subprocess that injects
 * Sutando identity and user context from the memory system.
 */

import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

function defaultMemoryDir(): string {
    const repo = resolve(join(import.meta.dirname, '..'));
    const slug = repo.replace(/\//g, '-');
    return join(homedir(), '.claude', 'projects', slug, 'memory');
}

const MEMORY_DIR = process.env.SUTANDO_MEMORY_DIR || defaultMemoryDir();
const REPO_DIR = new URL('..', import.meta.url).pathname.replace(/\/$/, '');

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
	const buildLog = join(REPO_DIR, 'build_log.md');
	if (existsSync(buildLog)) {
		try {
			const content = readFileSync(buildLog, 'utf-8');
			const scoreMatch = content.match(/\*\*Score: (.+?)\*\*/);
			if (scoreMatch) {
				lines.push(`SYSTEM STATUS: ${scoreMatch[1]}`, '');
			}
		} catch { /* best effort */ }
	}

	// Read recent build log activity (first date header + items)
	if (existsSync(buildLog)) {
		try {
			const content = readFileSync(buildLog, 'utf-8');
			const dateMatch = content.match(/## \d{4}-\d{2}-\d{2} — .+/);
			const items = content.match(/^- \*\*.+?\*\*.*/gm);
			if (dateMatch && items) {
				lines.push('RECENT ACTIVITY:', dateMatch[0].replace('## ', '  '), ...items.slice(0, 5).map(i => '  ' + i), '');
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
