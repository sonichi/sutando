/**
 * Active artifact cache — load a file once, answer repeated queries from in-process memory.
 *
 * Reduces voice Q&A latency on document-review sessions from ~80s per turn
 * (task-bridge round-trip) to ~300ms (in-process lookup). Voice-agent calls
 * clearActiveArtifact() on session end to prevent cross-session state leaks.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { extname } from 'node:path';
import { homedir } from 'node:os';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });

interface Section {
	header: string;
	start: number; // 0-indexed line, inclusive
	end: number;   // exclusive
}

interface ActiveArtifact {
	path: string;
	ext: string;
	content: string;
	lines: string[];
	sections: Section[];
	loadedAt: string;
	nChars: number;
}

let activeArtifact: ActiveArtifact | null = null;

/** Called by voice-agent on session end to prevent cross-session state leaks. */
export function clearActiveArtifact(): void {
	if (activeArtifact) {
		console.log(`${ts()} [ArtifactCache] Cleared (was: ${activeArtifact.path})`);
		activeArtifact = null;
	}
}

function expandPath(rawPath: string): string {
	return rawPath
		.replace(/^~/, homedir())
		.replace(/\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)/g, (_, a, b) => {
			return process.env[a || b] ?? '';
		});
}

function extractText(filePath: string, ext: string): string {
	if (ext === '.pdf') {
		try {
			return execFileSync('pdftotext', ['-layout', filePath, '-'], {
				encoding: 'utf8',
				maxBuffer: 10 * 1024 * 1024,
			});
		} catch {
			// pdftotext unavailable or failed — fall through to raw read
		}
	}
	return readFileSync(filePath, 'utf8');
}

const MD_HEADER_RE = /^#{1,3} /;
const CODE_SECTION_RE = /^(export\s+)?(class |function |async function |const \w+ (=|:))/;

function splitSections(lines: string[], ext: string): Section[] {
	const isMd = ext === '.md' || ext === '.txt';
	const isCode = ['.ts', '.js', '.py', '.sh'].includes(ext);
	if (!isMd && !isCode) return [];

	const matchFn = isMd
		? (line: string) => MD_HEADER_RE.test(line)
		: (line: string) => CODE_SECTION_RE.test(line.trimStart());

	const sections: Section[] = [];
	let sectionStart = -1;
	let sectionHeader = '';

	for (let i = 0; i < lines.length; i++) {
		if (matchFn(lines[i])) {
			if (sectionStart >= 0) {
				sections.push({ header: sectionHeader, start: sectionStart, end: i });
			}
			sectionStart = i;
			sectionHeader = lines[i].trim();
		}
	}
	if (sectionStart >= 0) {
		sections.push({ header: sectionHeader, start: sectionStart, end: lines.length });
	}
	return sections;
}

function buildSummary(artifact: ActiveArtifact): string {
	const top8 = artifact.sections.slice(0, 8).map(s => s.header).join(' | ');
	const overflow = artifact.sections.length > 8 ? ` … (${artifact.sections.length} total)` : '';
	const sectionDesc = artifact.sections.length > 0
		? `Sections: ${top8}${overflow}`
		: 'No section headers detected.';
	return `${artifact.nChars} chars, ${artifact.lines.length} lines. ${sectionDesc}`;
}

function findExcerpt(
	artifact: ActiveArtifact,
	query: string,
): { excerpt: string; section?: string; line_range: [number, number] } {
	const queryLower = query.toLowerCase();
	const words = queryLower.split(/\s+/).filter(w => w.length > 3);

	// 1. Try section header match first
	const sectionMatch = artifact.sections.find(s => {
		const h = s.header.toLowerCase();
		return h.includes(queryLower) || words.some(w => h.includes(w));
	});

	if (sectionMatch) {
		const end = Math.min(sectionMatch.end, sectionMatch.start + 40);
		return {
			excerpt: artifact.lines.slice(sectionMatch.start, end).join('\n'),
			section: sectionMatch.header,
			line_range: [sectionMatch.start + 1, end],
		};
	}

	// 2. Full-content keyword scan — find the first dense cluster of matching lines
	const matchingLines: number[] = [];
	for (let i = 0; i < artifact.lines.length && matchingLines.length < 30; i++) {
		const lineLower = artifact.lines[i].toLowerCase();
		if (lineLower.includes(queryLower) || words.some(w => lineLower.includes(w))) {
			matchingLines.push(i);
		}
	}

	if (matchingLines.length === 0) {
		return { excerpt: `No matches found for "${query}".`, line_range: [0, 0] };
	}

	const first = matchingLines[0];
	const start = Math.max(0, first - 5);
	const end = Math.min(artifact.lines.length, first + 20);
	const enclosing = [...artifact.sections].reverse().find(s => s.start <= first);

	return {
		excerpt: artifact.lines.slice(start, end).join('\n'),
		section: enclosing?.header,
		line_range: [start + 1, end],
	};
}

export const setActiveArtifactTool: ToolDefinition = {
	name: 'set_active_artifact',
	description:
		'Load a local file into the in-session cache for fast repeated Q&A. ' +
		'Use at the start of a document-review or code-review session ("open paper 1093", "let\'s look at task-bridge.ts"). ' +
		'Supports .pdf (pdftotext), .md/.txt, .ts/.js/.py/.sh, and any text file. ' +
		'After loading, use query_active_artifact — no task-bridge round-trip needed. ' +
		'Swap files by calling set_active_artifact again; clear with clear_active_artifact.',
	parameters: z.object({
		path: z.string().describe('Absolute or ~ path to the file to load.'),
	}),
	execution: 'inline',
	async execute(args) {
		const { path: rawPath } = args as { path: string };
		console.log(`${ts()} [ArtifactCache] set_active_artifact (path=${rawPath})`);
		try {
			const filePath = expandPath(rawPath);
			if (!existsSync(filePath)) {
				return { error: `File not found: ${filePath}` };
			}
			const ext = extname(filePath).toLowerCase();
			const content = extractText(filePath, ext);
			const lines = content.split('\n');
			const sections = splitSections(lines, ext);
			activeArtifact = { path: filePath, ext, content, lines, sections, loadedAt: new Date().toISOString(), nChars: content.length };
			console.log(`${ts()} [ArtifactCache] Loaded ${filePath} (${content.length} chars, ${sections.length} sections)`);
			return { artifact_id: filePath, summary: buildSummary(activeArtifact), n_chars: content.length, n_sections: sections.length };
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			console.error(`${ts()} [ArtifactCache] Error: ${msg}`);
			return { error: `Failed to load: ${msg}` };
		}
	},
};

export const queryActiveArtifactTool: ToolDefinition = {
	name: 'query_active_artifact',
	description:
		'Search the currently-loaded artifact (see set_active_artifact) for content matching a query. ' +
		'Use for any follow-up question about the same document: sections, keywords, line references. ' +
		'Fast — in-process, no task-bridge round-trip. Returns a relevant excerpt with line range. ' +
		'Error if no artifact is loaded.',
	parameters: z.object({
		query: z.string().describe('Section name, keyword, question, or line reference — e.g. "§4 baselines", "contribution", "what is W3".'),
	}),
	execution: 'inline',
	async execute(args) {
		const { query } = args as { query: string };
		console.log(`${ts()} [ArtifactCache] query_active_artifact (query=${query})`);
		if (!activeArtifact) {
			return { error: 'No active artifact. Call set_active_artifact first.' };
		}
		return { artifact_id: activeArtifact.path, ...findExcerpt(activeArtifact, query) };
	},
};

export const clearActiveArtifactTool: ToolDefinition = {
	name: 'clear_active_artifact',
	description:
		'Clear the active artifact from the session cache. ' +
		'Use when the conversation topic shifts, or the user is done with the current document. ' +
		'Voice-agent also clears automatically on session disconnect.',
	parameters: z.object({}),
	execution: 'inline',
	async execute(_args) {
		const had = activeArtifact?.path ?? null;
		clearActiveArtifact();
		return { ok: true, cleared: had };
	},
};
