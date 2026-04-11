/**
 * Voice → Claude Code session bridge.
 *
 * work writes task file directly (inline, no subagent).
 * The main Claude Code session picks it up via fswatch, executes with
 * full permissions, and writes result file.
 * The voice agent's node process watches for result file and
 * injects the result into the Gemini conversation.
 */

import { writeFileSync, readFileSync, existsSync, unlinkSync, mkdirSync, readdirSync, appendFileSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const REPO_DIR = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const TASK_DIR = join(REPO_DIR, 'tasks');
const RESULT_DIR = join(REPO_DIR, 'results');
const CONVERSATION_LOG = join(REPO_DIR, 'conversation.log');

// Ensure dirs exist
mkdirSync(TASK_DIR, { recursive: true });
mkdirSync(RESULT_DIR, { recursive: true });

function ts(): string { return new Date().toISOString().slice(11, 23); }

// ---------------------------------------------------------------------------
// Task status notifications — sent to the web client
// ---------------------------------------------------------------------------

let _sendTaskStatus: ((taskId: string, status: string, text: string, result?: string) => void) | null = null;
const _deliveredResults = new Set<string>();

const TASK_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes
const _pendingTasks = new Map<string, number>(); // taskId → submission epoch ms
const _apiToken = process.env.SUTANDO_API_TOKEN || '';
function _apiHeaders(): Record<string, string> {
	const h: Record<string, string> = { 'Content-Type': 'application/json' };
	if (_apiToken) h['Authorization'] = `Bearer ${_apiToken}`;
	return h;
}

/** Register a callback to send task status to the web client. */
export function setTaskStatusCallback(fn: (taskId: string, status: string, text: string, result?: string) => void): void {
	_sendTaskStatus = fn;
}

// ---------------------------------------------------------------------------
// Main agent tool — writes task file directly, no subagent needed
// ---------------------------------------------------------------------------

export const workTool: ToolDefinition = {
	name: 'work',
	description:
		'Do the work. Call this for anything beyond simple greetings — questions, actions, ' +
		'research, writing, translation, file changes, system queries, explanations, analysis. ' +
		'This is how Sutando thinks and acts. Results are spoken back when ready.',
	parameters: z.object({
		task: z.string().describe('Full description of the task to perform'),
	}),
	execution: 'inline',
	async execute(args) {
		const { task } = args as { task: string };

		// Redirect pure screen-viewing tasks to inline tools (faster, no round-trip)
		// Narrow match: only "describe/look at my screen" — not scroll, screenshot,
		// or screen-related tasks that the brain should handle.
		const screenViewOnly = /\b(describe\s+(my\s+)?screen|what.s on\s+(my\s+)?screen|look at\s+(my\s+)?screen)\b/i;
		if (screenViewOnly.test(task)) {
			return { status: 'rejected', message: 'Use describe_screen inline tool directly for screen viewing.' };
		}

		// Check if the watcher (Claude Code brain) is running
		let watcherOnline = false;
		try {
			const { execSync } = await import('node:child_process');
			const watcherRunning = execSync('pgrep -f "watch-tasks" 2>/dev/null', { encoding: 'utf-8' }).trim();
			watcherOnline = !!watcherRunning;
		} catch {
			// pgrep returns exit code 1 if no match
		}
		if (!watcherOnline) {
			console.log(`${ts()} [TaskBridge] WARNING: watcher offline — task will be queued for next cron pass`);
		}

		const taskId = `task-${Date.now()}`;
		const timestamp = new Date().toISOString();
		const content = `id: ${taskId}\ntimestamp: ${timestamp}\ntask: ${task}\nreminder: Process ALL .txt files in tasks/ before restarting the watcher. Use: bash src/watch-tasks.sh (run_in_background: true)\n`;
		writeFileSync(join(TASK_DIR, `${taskId}.txt`), content);
		_pendingTasks.set(taskId, Date.now());
		console.log(`${ts()} [TaskBridge] Task ${taskId}: ${task.slice(0, 100)}`);
		_sendTaskStatus?.(taskId, 'working', task.slice(0, 60));
		return {
			status: 'pending',
			taskId,
			message: watcherOnline
				? 'Task has been queued and is being processed. The result will be spoken when ready. Do NOT tell the user the task is done — say you are working on it.'
				: 'Task has been saved. The processing engine will pick it up on its next pass (within a few minutes). Tell the user the task is queued and will be handled shortly.',
		};
	},
};

// ---------------------------------------------------------------------------
// Cancel task tool — removes the most recent pending task
// ---------------------------------------------------------------------------

export const cancelTask: ToolDefinition = {
	name: 'cancel_task',
	description:
		'Cancel a pending task. Use when the user says cancel, nevermind, stop, or forget it.',
	parameters: z.object({}),
	execution: 'inline',
	async execute() {
		try {
			const files = readdirSync(TASK_DIR).filter(f => f.endsWith('.txt')).sort();
			if (files.length === 0) {
				return { status: 'nothing_to_cancel', message: 'No pending tasks to cancel.' };
			}
			const mostRecent = files[files.length - 1];
			const taskId = mostRecent.replace('.txt', '');
			unlinkSync(join(TASK_DIR, mostRecent));
			_pendingTasks.delete(taskId);
			console.log(`${ts()} [TaskBridge] Cancelled task ${taskId}`);
			_sendTaskStatus?.(taskId, 'cancelled', 'Task cancelled by user');
			// Notify agent-api
			try { fetch('http://localhost:7843/task-done', { method: 'POST', headers: _apiHeaders(), body: JSON.stringify({ taskId, result: 'Cancelled by user' }) }).catch(() => {}); } catch {}
			return { status: 'cancelled', taskId, message: 'Cancelled the most recent task.' };
		} catch (err) {
			return { status: 'error', message: `Failed to cancel: ${err instanceof Error ? err.message : err}` };
		}
	},
};

// ---------------------------------------------------------------------------
// Result watcher — call this once at startup to watch for results
// and inject them into the conversation via a callback
// ---------------------------------------------------------------------------

/** Append a message to the persistent conversation log. */
export function logConversation(role: string, text: string): void {
	const line = `${new Date().toISOString()}|${role}|${text.replace(/\n/g, ' ').slice(0, 200)}\n`;
	try { appendFileSync(CONVERSATION_LOG, line); } catch { /* best effort */ }
}

/** Append a session-end boundary marker. Used by voice-agent's
 *  endSession tool so that getRecentConversation() can trim its
 *  replay window at the last session boundary — preventing goodbye
 *  text from a prior session from contaminating the reconnect
 *  greeting. Replaces the pattern-match filter that got defeated
 *  multiple times on 2026-04-09 (commits 1-6 of PR #257).
 *
 *  Format: ISO-ts|SESSION_END|<reason>
 *  The `SESSION_END` sentinel is unique so the reader can locate
 *  it without regex gymnastics. */
export function logSessionBoundary(reason: string = 'user_goodbye'): void {
	const line = `${new Date().toISOString()}|SESSION_END|${reason}\n`;
	try { appendFileSync(CONVERSATION_LOG, line); } catch { /* best effort */ }
}

/** Read recent conversation entries from disk, trimming at the most
 *  recent SESSION_END marker. Survives restarts. Returns at most
 *  `count` entries from the current session only — a cleanly-ended
 *  prior session has no meaningful follow-up context. */
export function getRecentConversation(count = 10): string {
	if (!existsSync(CONVERSATION_LOG)) return '';
	try {
		const allLines = readFileSync(CONVERSATION_LOG, 'utf-8').trim().split('\n');
		// Find the last SESSION_END marker and keep only lines after it
		let lastBoundary = -1;
		for (let i = allLines.length - 1; i >= 0; i--) {
			if (allLines[i].includes('|SESSION_END|')) {
				lastBoundary = i;
				break;
			}
		}
		const currentSession = lastBoundary >= 0 ? allLines.slice(lastBoundary + 1) : allLines;
		const lines = currentSession.slice(-count);
		return lines.map(l => {
			const [, role, text] = l.split('|', 3);
			return role && text ? `${role}: ${text}` : '';
		}).filter(Boolean).join('\n');
	} catch { return ''; }
}

const CONTEXT_DROP_FILE = join(REPO_DIR, 'context-drop.txt');
const NOTE_VIEWING_FILE = '/tmp/sutando-note-viewing.json';

/**
 * Watch for context-drop.txt and inject into Gemini conversation.
 * Called once at startup. When user drops context via keyboard shortcut,
 * it gets sent to Gemini so it knows about it.
 */
export function startContextDropWatcher(onContextDrop: (content: string) => void): void {
	console.log(`${ts()} [TaskBridge] Watching for context drops`);
	setInterval(() => {
		if (existsSync(CONTEXT_DROP_FILE)) {
			try {
				const content = readFileSync(CONTEXT_DROP_FILE, 'utf-8').trim();
				if (content) {
					console.log(`${ts()} [TaskBridge] Context drop detected: ${content.slice(0, 100)}`);
					// Always write a task for sutando-core (reliable path)
					mkdirSync(TASK_DIR, { recursive: true });
					const taskId = `task-${Date.now()}`;
					writeFileSync(join(TASK_DIR, `${taskId}.txt`),
						`id: ${taskId}\ntimestamp: ${new Date().toISOString()}\ntask: User dropped context via hotkey. Process this:\n${content}\n`);
					unlinkSync(CONTEXT_DROP_FILE);
					// Also inject into Gemini if available
					onContextDrop(content);
				}
			} catch { /* file might be in transit */ }
		}
	}, 2000);
}

/**
 * Watch for note-view events and inject into Gemini conversation.
 * The web client writes {slug, content, ts} to /tmp/sutando-note-viewing.json
 * whenever the user opens a note in the UI. This watcher reads the latest
 * event and hands it to the voice agent so Gemini knows what the user is
 * currently looking at — lets questions like "what does this note say about
 * X" work without the user dictating the note path.
 *
 * Unlike the context-drop watcher, this does NOT write a task file: a note
 * view is ambient UI state, not an action to execute. We also debounce by
 * tracking the last event's timestamp so that repeatedly viewing the same
 * note doesn't re-inject.
 */
let lastNoteViewingTs = '';
// Track the last event we *logged* separately from the last we *handled*,
// so that when the keep-pending-on-disconnect path (PR #246) retries an
// event every 2s, we emit only one "Note view detected" line per unique
// event.ts. Without this, voice-agent.log fills with ~30 identical lines
// per minute whenever the user opens a note while voice is disconnected.
let lastNoteViewingLoggedTs = '';
/**
 * Read the current note-viewing event from disk, if any. Used for
 * on-reconnect delivery so the voice agent can catch up on what the user
 * is looking at without waiting for a fresh click.
 */
export function readCurrentNoteViewing(): { slug: string; content: string; ts: string } | null {
	if (!existsSync(NOTE_VIEWING_FILE)) return null;
	try {
		const raw = readFileSync(NOTE_VIEWING_FILE, 'utf-8').trim();
		if (!raw) return null;
		const event = JSON.parse(raw) as { slug?: string; content?: string; ts?: string };
		if (!event.slug || !event.content || !event.ts) return null;
		return { slug: event.slug, content: event.content, ts: event.ts };
	} catch {
		return null;
	}
}

export function startNoteViewingWatcher(
	onNoteView: (slug: string, content: string) => boolean | void,
): void {
	console.log(`${ts()} [TaskBridge] Watching for note views (${NOTE_VIEWING_FILE})`);
	setInterval(() => {
		const event = readCurrentNoteViewing();
		if (!event) return;
		if (event.ts === lastNoteViewingTs) return;  // already handled
		if (event.ts !== lastNoteViewingLoggedTs) {
			console.log(`${ts()} [TaskBridge] Note view detected: ${event.slug}`);
			lastNoteViewingLoggedTs = event.ts;
		}
		const handled = onNoteView(event.slug, event.content);
		// Only mark as handled if the callback actually delivered it. This
		// lets a voice-disconnected callback return false/void-with-falsy
		// and we'll try again on the next poll — which matters when a
		// reconnect handler also calls back through here.
		if (handled !== false) lastNoteViewingTs = event.ts;
	}, 2000);
}

/**
 * Reset the note-viewing debounce so a subsequent poll re-delivers the
 * current event. Called from the voice session on reconnect so that a
 * note the user was already looking at gets injected fresh.
 */
export function resetNoteViewingDebounce(): void {
	lastNoteViewingTs = '';
	// Also reset the logged-ts so the next delivery attempt logs again —
	// a reconnect is a meaningful event that should show up in the log.
	lastNoteViewingLoggedTs = '';
}

export function startResultWatcher(onResult: (result: string) => void, isClientConnected: () => boolean): void {
	console.log(`${ts()} [TaskBridge] Watching for results in ${RESULT_DIR}`);

	// Check every 2 seconds for new result files
	setInterval(() => {
		// Check for timed-out tasks — runs every interval regardless of result files
		for (const [taskId, submittedAt] of _pendingTasks) {
			if (Date.now() - submittedAt > TASK_TIMEOUT_MS) {
				_pendingTasks.delete(taskId);
				console.error(`${ts()} [TaskBridge] Task ${taskId} timed out after ${TASK_TIMEOUT_MS / 1000}s`);
				_sendTaskStatus?.(taskId, 'timeout', 'Task timed out — core agent may be unresponsive');
				onResult(`[Task timed out after ${Math.floor(TASK_TIMEOUT_MS / 60000)} minutes. The processing engine may need to be restarted.]`);
			}
		}

		try {
			const files = readdirSync(RESULT_DIR).filter(f => f.endsWith('.txt')).sort();
			if (files.length === 0) return;

			// Only deliver if a client is connected — otherwise keep files queued
			if (!isClientConnected()) {
				return;
			}

			for (const file of files) {
				if (_deliveredResults.has(file)) continue;
				const path = join(RESULT_DIR, file);
				const result = readFileSync(path, 'utf-8').trim();
				if (result) {
					const taskId = file.replace('.txt', '');
					console.log(`${ts()} [TaskBridge] Result ${file}: ${result.slice(0, 100)}`);
					_sendTaskStatus?.(taskId, 'done', result.slice(0, 60), result);
					_deliveredResults.add(file);
					_pendingTasks.delete(taskId);
					logConversation('core-agent', `[task:${taskId}] ${result.slice(0, 200)}`);
					onResult(result);
					// Notify agent-api directly, then delete file
					try {
						fetch('http://localhost:7843/task-done', {
							method: 'POST',
							headers: _apiHeaders(),
							body: JSON.stringify({ taskId, result }),
						}).catch(() => {});
					} catch {}
					setTimeout(() => { try { unlinkSync(path); } catch {} }, 10_000);
				}
			}
		} catch {
			// Directory might not exist yet or file in transit
		}
	}, 2000);
}
