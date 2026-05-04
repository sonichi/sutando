/**
 * Voice → Claude Code session bridge.
 *
 * work writes task file directly (inline, no subagent).
 * The main Claude Code session picks it up via fswatch, executes with
 * full permissions, and writes result file.
 * The voice agent's node process watches for result file and
 * injects the result into the Gemini conversation.
 */

import { writeFileSync, readFileSync, existsSync, unlinkSync, mkdirSync, readdirSync, appendFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';

const REPO_DIR = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const TASK_DIR = join(REPO_DIR, 'tasks');
const RESULT_DIR = join(REPO_DIR, 'results');
const STATE_DIR = join(REPO_DIR, 'state');
const CONVERSATION_LOG = join(REPO_DIR, 'conversation.log');
const OWNER_ACTIVITY_FILE = join(STATE_DIR, 'last-owner-activity.json');

/** Record that the owner was active on <channel> right now. Atomic write
 * via tmp-then-rename. Read by the proactive-loop status-aware-pivot rule.
 * See notes/team-proposal-coord-loop-2026-04-20.md. */
function writeOwnerActivity(channel: string, summary: string): void {
	try {
		mkdirSync(STATE_DIR, { recursive: true });
		const payload = {
			ts: Math.floor(Date.now() / 1000),
			channel,
			summary: summary.slice(0, 80),
		};
		const tmp = OWNER_ACTIVITY_FILE + '.tmp';
		writeFileSync(tmp, JSON.stringify(payload));
		renameSync(tmp, OWNER_ACTIVITY_FILE);
	} catch (e) {
		// Non-fatal — activity-state is best-effort
		console.log(`${ts()} [TaskBridge] owner-activity write failed: ${e}`);
	}
}

/** Archive a task/result file into archive/<kind>/YYYY-MM/ instead of
 * deleting. Chi's 2026-04-18 ask: "instead of deleting we should archive
 * the tasks. It can be useful for self-improving". Silent on failure;
 * fall back to unlink so the system never leaves stale files behind. */
function archiveFile(srcPath: string, kind: 'tasks' | 'results', taskId: string): void {
	try {
		if (!existsSync(srcPath)) return;
		const ym = new Date().toISOString().slice(0, 7); // YYYY-MM
		const destDir = join(REPO_DIR, kind, 'archive', ym);
		mkdirSync(destDir, { recursive: true });
		renameSync(srcPath, join(destDir, `${taskId}.txt`));
	} catch (err) {
		try { unlinkSync(srcPath); } catch { /* ignore */ }
	}
}

// Ensure dirs exist
mkdirSync(TASK_DIR, { recursive: true });
mkdirSync(RESULT_DIR, { recursive: true });

function ts(): string { return new Date().toISOString().slice(11, 23); }

// ---------------------------------------------------------------------------
// Task status notifications — sent to the web client
// ---------------------------------------------------------------------------

let _sendTaskStatus: ((taskId: string, status: string, text: string, result?: string) => void) | null = null;
const _deliveredResults = new Set<string>();

const DEFAULT_TASK_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes default
// Per-task pending state: submission epoch, timeout (ms), and whether to
// emit a Discord DM to the owner if this task hits its timeout. dm_on_timeout
// defaults to false (silent timeout — Susan's PR #578 contract). Voice agent
// can flip it true on critical tasks to get a fallback notification.
type PendingTask = { submittedAt: number; timeoutMs: number; dmOnTimeout: boolean };
const _pendingTasks = new Map<string, PendingTask>();

/** True if the task file (in tasks/, tasks/processed/, or tasks/archive/)
 * is voice-originated (channel_id: local-voice). Used by the result watcher
 * to decide whether to forward an unsent result to Discord DM when voice is
 * offline. Returns false on missing file or parse error — bias toward not
 * forwarding to keep Susan-rejected always-DM behavior off by default for
 * non-voice tasks. */
function _isVoiceTask(taskId: string): boolean {
	const candidates = [
		join(TASK_DIR, `${taskId}.txt`),
		join(TASK_DIR, 'processed', `${taskId}.txt`),
		join(TASK_DIR, 'archive', `${taskId}.txt`),
	];
	for (const p of candidates) {
		if (!existsSync(p)) continue;
		try {
			const body = readFileSync(p, 'utf-8');
			return body.split('\n').some(l => l.startsWith('channel_id: local-voice') || l.startsWith('source: voice'));
		} catch {}
	}
	return false;
}
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
		timeout_minutes: z
			.number()
			.optional()
			.describe(
				'Per-task timeout in minutes. Default 10. Pass a larger value (e.g. 30) for ' +
				'multi-step jobs like rendering, batch encoding, or long research. Pass 0 for ' +
				'no timeout — use sparingly, only when the user explicitly asks for a long ' +
				'autonomous job that may legitimately take hours.'
			),
		dm_on_timeout: z
			.boolean()
			.optional()
			.describe(
				'If true, send a Discord DM to the owner when this task hits its timeout. ' +
				'Default false (silent UI-only timeout, per Susan PR #578). Use only for ' +
				'tasks the user has explicitly flagged as critical. The Chi-override to ' +
				'default-true (2026-05-03 06:00 PT) was reverted at 06:47 PT after Chi flagged ' +
				'a timeout DM that shouldn\'t have gone through.'
			),
	}),
	execution: 'inline',
	async execute(args) {
		const { task, timeout_minutes, dm_on_timeout } = args as {
			task: string;
			timeout_minutes?: number;
			dm_on_timeout?: boolean;
		};

		// Redirect pure screen-viewing tasks to inline tools (faster, no round-trip)
		// Narrow match: only "describe/look at my screen" — not scroll, screenshot,
		// or screen-related tasks that the brain should handle.
		const screenViewOnly = /\b(describe\s+(my\s+)?screen|what.s on\s+(my\s+)?screen|look at\s+(my\s+)?screen)\b/i;
		if (screenViewOnly.test(task)) {
			return { status: 'rejected', message: 'Use describe_screen inline tool directly for screen viewing.' };
		}

		// Fast path: handle known patterns inline for ~3s vs ~15s via file bridge.
		// Same pattern as conversation-server's tryFastPath.
		const concatMatch = /\b(prepend|concatenat|concat|image.*video|video.*image)\b/i.test(task);
		if (concatMatch) {
			try {
				const { execSync } = await import('node:child_process');
				const image = execSync('ls -t /tmp/discord-inbox/*.jpg /tmp/discord-inbox/*.png 2>/dev/null | head -1', { timeout: 3000 }).toString().trim();
				const video = execSync('ls -t /tmp/sutando-recording-*-narrated-subtitled.mov /tmp/sutando-recording-*-narrated.mov /tmp/sutando-recording-*.mov 2>/dev/null | head -1', { timeout: 3000 }).toString().trim();
				if (image && video) {
					const result = execSync(`bash ~/.claude/skills/video-concat/scripts/prepend-image.sh "${image}" "${video}" 3`, { timeout: 60000 }).toString().trim();
					const parsed = JSON.parse(result);
					return { status: 'done', result: `Video with image prepended: ${parsed.output} (${parsed.size_mb}MB)` };
				}
			} catch (e) { console.log(`${ts()} [TaskBridge] fast path concat failed: ${e}`); }
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
		const ownerId = process.env.SUTANDO_DM_OWNER_ID || 'voice-local';
		const content =
			`id: ${taskId}\n` +
			`timestamp: ${timestamp}\n` +
			`task: ${task}\n` +
			`source: voice\n` +
			`channel_id: local-voice\n` +
			`user_id: ${ownerId}\n` +
			`access_tier: owner\n`;
		writeFileSync(join(TASK_DIR, `${taskId}.txt`), content);
		// Resolve per-task timeout. 0 → no timeout. Negative or NaN → default.
		// Cap at 6 hours to prevent runaway pending-state if the voice agent
		// hallucinates a giant value.
		let timeoutMs = DEFAULT_TASK_TIMEOUT_MS;
		if (typeof timeout_minutes === 'number') {
			if (timeout_minutes === 0) timeoutMs = 0;
			else if (timeout_minutes > 0) timeoutMs = Math.min(timeout_minutes, 360) * 60 * 1000;
		}
		// Default FALSE (Susan PR #578 silent-timeout contract restored after
		// Chi's 2026-05-03 06:00 override was reverted at 06:47 — the always-on
		// default was producing unwanted DMs). Caller must explicitly pass
		// dm_on_timeout: true on critical tasks where they want the fallback.
		_pendingTasks.set(taskId, { submittedAt: Date.now(), timeoutMs, dmOnTimeout: dm_on_timeout === true });
		// Record owner activity for status-aware-pivot in proactive loop
		writeOwnerActivity('voice', task);
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

// cancelTask tool moved — canonical version is `cancelTaskTool` in inline-tools.ts.

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

/** Seconds since the most recent user/assistant turn. Walks the log
 *  backwards, skipping `core-agent` task-result lines (written by the
 *  task-bridge result watcher whenever the proactive loop or any
 *  background task posts a result) and `SESSION_END` markers — those
 *  are not user/assistant dialogue, and a recent one would falsely
 *  make a long-away user look like a quick reconnect. Stops at the
 *  most recent SESSION_END so we don't reach back into a cleanly-ended
 *  prior session. Returns null if no log exists or no user/assistant
 *  turn is found in the current session. */
export function getSecondsSinceLastTurn(): number | null {
	if (!existsSync(CONVERSATION_LOG)) return null;
	try {
		const content = readFileSync(CONVERSATION_LOG, 'utf-8').trim();
		if (!content) return null;
		const lines = content.split('\n');
		for (let i = lines.length - 1; i >= 0; i--) {
			const role = lines[i].split('|')[1];
			if (role === 'SESSION_END') return null;
			if (role !== 'user' && role !== 'assistant') continue;
			const ts = Date.parse(lines[i].split('|')[0]);
			if (Number.isNaN(ts)) return null;
			return (Date.now() - ts) / 1000;
		}
		return null;
	} catch { return null; }
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
					const ownerId = process.env.SUTANDO_DM_OWNER_ID || 'voice-local';
					writeFileSync(
						join(TASK_DIR, `${taskId}.txt`),
						`id: ${taskId}\n` +
						`timestamp: ${new Date().toISOString()}\n` +
						`task: User dropped context via hotkey. Process this:\n${content}\n` +
						`source: context-drop\n` +
						`channel_id: local-hotkey\n` +
						`user_id: ${ownerId}\n` +
						`access_tier: owner\n`,
					);
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
		for (const [taskId, pending] of _pendingTasks) {
			const { submittedAt, timeoutMs, dmOnTimeout } = pending;
			// timeoutMs === 0 means "no timeout" — skip the check entirely.
			if (timeoutMs === 0) continue;
			if (Date.now() - submittedAt > timeoutMs) {
				_pendingTasks.delete(taskId);
				// Read the task body (or a snippet of it) so the timeout message
				// can identify which task timed out — the prior generic "[Task
				// timed out]" string left no clue when multiple tasks were in
				// flight. Snippet is bounded to 80 chars to keep the voice
				// narration short.
				const taskFile = join(TASK_DIR, `${taskId}.txt`);
				let taskSnippet = '';
				if (existsSync(taskFile)) {
					try {
						const body = readFileSync(taskFile, 'utf-8');
						const taskLine = body.split('\n').find(l => l.startsWith('task:'));
						const raw = (taskLine ? taskLine.slice(5) : '').trim();
						taskSnippet = raw.length > 80 ? raw.slice(0, 77) + '...' : raw;
					} catch {}
				}
				console.error(`${ts()} [TaskBridge] Task ${taskId} (${taskSnippet || '?'}) timed out after ${timeoutMs / 1000}s`);
				const statusMsg = taskSnippet
					? `Task '${taskSnippet}' timed out — core agent may be unresponsive`
					: 'Task timed out — core agent may be unresponsive';
				_sendTaskStatus?.(taskId, 'timeout', statusMsg);
				const minutes = Math.floor(timeoutMs / 60000);
				const userMsg = taskSnippet
					? `[Task ${taskId} ('${taskSnippet}') timed out after ${minutes} minutes. The processing engine may need to be restarted.]`
					: `[Task ${taskId} timed out after ${minutes} minutes. The processing engine may need to be restarted.]`;
				onResult(userMsg);
				// Move the task file out of tasks/ so /tasks/active stops listing it
				// as 'working' forever. (Without this, dedup-orphan tasks left behind
				// after a consolidated reply pile up in the UI as stuck spinners.)
				if (existsSync(taskFile)) {
					try {
						const processedDir = join(TASK_DIR, 'processed');
						mkdirSync(processedDir, { recursive: true });
						renameSync(taskFile, join(processedDir, `${taskId}.txt`));
					} catch (e) {
						console.error(`${ts()} [TaskBridge] Failed to archive timed-out task ${taskId}:`, e);
					}
				}
				// Discord DM fallback (opt-in via dm_on_timeout). Default off per
				// Susan's PR #578 contract — silent timeout. We emit by writing
				// a proactive-*.txt file; discord-bridge.py poll_proactive sends
				// it to the owner's DM.
				if (dmOnTimeout) {
					try {
						const proactiveTs = Math.floor(Date.now() / 1000);
						const proactivePath = join(RESULT_DIR, `proactive-timeout-${taskId}-${proactiveTs}.txt`);
						const dmBody = taskSnippet
							? `⏱ Task '${taskSnippet}' timed out after ${minutes}m. The processing engine may need to be restarted, or the task may need a longer timeout via timeout_minutes.`
							: `⏱ Task ${taskId} timed out after ${minutes}m.`;
						writeFileSync(proactivePath, dmBody);
						console.log(`${ts()} [TaskBridge] Wrote DM-on-timeout proactive file for ${taskId}`);
					} catch (e) {
						console.error(`${ts()} [TaskBridge] Failed to emit DM-on-timeout for ${taskId}:`, e);
					}
				}
			}
		}

		try {
			const files = readdirSync(RESULT_DIR).filter(f => f.endsWith('.txt')).sort();
			if (files.length === 0) return;

			const clientConnected = isClientConnected();

			for (const file of files) {
				if (_deliveredResults.has(file)) continue;
				const path = join(RESULT_DIR, file);
				const result = readFileSync(path, 'utf-8').trim();
				if (!result) continue;
				const taskId = file.replace('.txt', '');
				// Deduped-marker result: agent consolidated this task's reply
				// into another task's result file. Mark this task done silently
				// and archive — no Discord post, no voice narration, no timeout.
				// Format: first line is "[deduped: <other-task-id>]" (rest of
				// file optional, displayed as the result body in the UI).
				if (file.startsWith('task-') && /^\s*\[deduped:\s*task-/i.test(result)) {
					console.log(`${ts()} [TaskBridge] ${taskId} is deduped marker; archiving silently`);
					_sendTaskStatus?.(taskId, 'done', result.slice(0, 60), result);
					_deliveredResults.add(file);
					_pendingTasks.delete(taskId);
					try {
						fetch('http://localhost:7843/task-done', {
							method: 'POST',
							headers: _apiHeaders(),
							body: JSON.stringify({ taskId, result }),
						}).catch(() => {});
					} catch {}
					setTimeout(() => {
						archiveFile(path, 'results', taskId);
						const taskFile = join(TASK_DIR, `${taskId}.txt`);
						if (existsSync(taskFile)) archiveFile(taskFile, 'tasks', taskId);
					}, 5_000);
					continue;
				}
				// Voice client offline → forward voice-task results to Discord DM
				// via a proactive-result-*.txt file (poll_proactive in
				// discord-bridge.py picks it up and DMs the owner). Skips files
				// that aren't voice-originated tasks (Discord/Telegram bridges
				// handle their own deliveries via pending_replies).
				if (!clientConnected) {
					if (file.startsWith('task-') && _isVoiceTask(taskId)) {
						try {
							const proactiveTs = Math.floor(Date.now() / 1000);
							const proactivePath = join(RESULT_DIR, `proactive-result-${taskId}-${proactiveTs}.txt`);
							writeFileSync(proactivePath, result);
							console.log(`${ts()} [TaskBridge] Voice offline; forwarded ${taskId} result to Discord DM via ${proactivePath}`);
							_deliveredResults.add(file);
							_pendingTasks.delete(taskId);
							setTimeout(() => {
								archiveFile(path, 'results', taskId);
								const taskFile = join(TASK_DIR, `${taskId}.txt`);
								if (existsSync(taskFile)) archiveFile(taskFile, 'tasks', taskId);
							}, 10_000);
						} catch (e) {
							console.error(`${ts()} [TaskBridge] Failed to forward ${taskId} to Discord:`, e);
						}
					}
					// Other non-voice unsent results stay queued (their bridges deliver them)
					continue;
				}
				if (result) {
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
					setTimeout(() => {
						const taskIdFromFile = path.split('/').pop()!.replace('.txt', '');
						archiveFile(path, 'results', taskIdFromFile);
						// Also archive the originating task file so get_task_status
						// stops counting it as "queued" — voice agent reads
						// tasks/*.txt directly and otherwise sees stale files
						// (Chi reported "task done in UI but queued in voice"
						// on 2026-05-04 with 32 stale files in tasks/).
						const taskFile = join(TASK_DIR, `${taskIdFromFile}.txt`);
						if (existsSync(taskFile)) archiveFile(taskFile, 'tasks', taskIdFromFile);
					}, 10_000);
				}
			}
		} catch {
			// Directory might not exist yet or file in transit
		}
	}, 2000);
}
