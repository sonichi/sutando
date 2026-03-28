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
			console.log(`${ts()} [TaskBridge] Cancelled task ${taskId}`);
			_sendTaskStatus?.(taskId, 'cancelled', 'Task cancelled by user');
			// Notify agent-api
			try { fetch('http://localhost:7843/task-done', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ taskId, result: 'Cancelled by user' }) }).catch(() => {}); } catch {}
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

/** Read the last N conversation entries from disk. Survives restarts. */
export function getRecentConversation(count = 10): string {
	if (!existsSync(CONVERSATION_LOG)) return '';
	try {
		const lines = readFileSync(CONVERSATION_LOG, 'utf-8').trim().split('\n').slice(-count);
		return lines.map(l => {
			const [, role, text] = l.split('|', 3);
			return role && text ? `${role}: ${text}` : '';
		}).filter(Boolean).join('\n');
	} catch { return ''; }
}

const CONTEXT_DROP_FILE = join(REPO_DIR, 'context-drop.txt');

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

export function startResultWatcher(onResult: (result: string) => void, isClientConnected: () => boolean): void {
	console.log(`${ts()} [TaskBridge] Watching for results in ${RESULT_DIR}`);

	// Check every 2 seconds for new result files
	setInterval(() => {
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
					onResult(result);
					// Notify agent-api directly, then delete file
					try {
						fetch('http://localhost:7843/task-done', {
							method: 'POST',
							headers: { 'Content-Type': 'application/json' },
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
