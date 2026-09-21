/**
 * Voice → Claude Code session bridge.
 *
 * work writes task file directly (inline, no subagent).
 * The main Claude Code session picks it up via fswatch, executes with
 * full permissions, and writes result file.
 * The voice agent's node process watches for result file and
 * injects the result into the Gemini conversation.
 */

import { writeFileSync, readFileSync, existsSync, unlinkSync, mkdirSync, readdirSync, statSync, appendFileSync, renameSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveWorkspace } from './workspace_default.js';
import { tryStampText } from './task_envelope.js';
import { claudeHomePath } from './util_paths.js';
import { isSkipMarked, mayRetireSkipMarked, bodyIsSkipMarked, type TaskOrigin } from './skip_marker_ownership.js';
import { recordConversation, recordSessionBoundary } from './conversation-store.js';
import {
	emitTaskProcessed,
	selectBackend,
	type TaskDelegationService,
} from './task-delegation.js';

const REPO_DIR = resolveWorkspace();
const TASK_DIR = join(REPO_DIR, 'tasks');
const RESULT_DIR = join(REPO_DIR, 'results');
const STATE_DIR = join(REPO_DIR, 'state');
const CONVERSATION_LOG = join(REPO_DIR, 'logs', 'conversation.log');
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
		// Per-PID staging: last-owner-activity.json is written by five processes
		// (this task-bridge + the sparrow/discord/slack/telegram bridges). A shared
		// '.tmp' name lets two concurrent writers truncate and interleave the same
		// temp file, so the rename can publish torn JSON. A per-PID temp is never
		// shared; renameSync maps to an atomic rename(2). (sonichi/sutando#2222)
		const tmp = `${OWNER_ACTIVITY_FILE}.${process.pid}.tmp`;
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
	} catch {
		try { unlinkSync(srcPath); } catch { /* ignore */ }
	}
}

// TaskDelegationService seam (#1947): CORE_API_URL set → relay to the core
// host's agent-api (explicit positive config, Codex P1); otherwise local file
// I/O, byte-identical to the pre-seam writes. Local dirs are created by the
// LOCAL selection path only — a relay-mode voice host doesn't grow empty
// tasks/ + results/ dirs it will never use. Failure is loud (selectBackend).
const _delegation: TaskDelegationService = selectBackend(TASK_DIR, RESULT_DIR, archiveFile);

function ts(): string { return new Date().toISOString().slice(11, 23); }

/** U+200B — zero-width space; not whitespace, so it survives .trimStart(). */
const _ZWSP = '​';
// Kept in lockstep with local_task_protocol.KNOWN_HEADER_KEYS (the Python
// guard's source of truth). TS can't import the Python tuple, so this list is
// the mirror; injection-guard-sweep asserts parity so drift fails CI. Synced to
// the full 38-key set on the 2026-07-13 main merge (main widened the Python side
// from 14 → 38; the TS guard must defang the same keys or forged interaction_type:
// / attachments: / media_form: lines slip through here).
const _HEADER_KEYS = [
	'id', 'timestamp', 'session_scope', 'task', 'source', 'access_tier', 'user_id',
	'channel_id', 'priority', 'interaction_type', 'source_message_id',
	'channel_name', 'guild_name', 'attempts', 'sender_name', 'room_name',
	'parent_message_id', 'reply_chain_ids', 'reminder', 'author_name', 'author_id', 'chat_id',
	'thread_ts', 'reply_to_event', 'reply_to_me', 'reply_to_sender', 'addressed_to', 'callSid', 'caller',
	'thread_root', 'source_room_id', 'channel_kind',
	'receiving_instance',
	'from', 'call_sid', 'hint', 'instructions', 'transcript',
	'schedule_name', 'schedule_slot',
	'content_modalities', 'media_form', 'attachments', 'platform_card',
	'instance_id', 'collaborator', 'requested_worker', 'wire_source', 'picker_command', 'picker_args', 'hitl_click',
];
const _HEADER_RE = new RegExp(`^(?:${_HEADER_KEYS.join('|')})\\s*:`, 'i');
const _FENCE_RE = /^={3,}/;
// Every separator str.splitlines() / universal-newline readers treat as a
// line boundary — fold ALL to '\n' so the guard's line-set matches the
// reader's (else \v \f \x1c-\x1e \x85 \u2028 \u2029 smuggle a forged line past it).
const _LINE_SEP_RE = /\r\n|[\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/g;

/**
 * Defang user-supplied content before embedding in a task file.
 *
 * Prefixes any line that looks like a task-file header field or a
 * ===FENCE=== with U+200B so structural injection (access_tier forge,
 * system-instruction fence) cannot succeed. Idempotent; folds every str.splitlines() separator (not just CR/CRLF).
 * TypeScript mirror of src/task_body_guard.py:confine_user_content().
 */
function confineUserContent(text: string): string {
	if (!text) return text;
	const normalized = text.replace(_LINE_SEP_RE, '\n');
	return normalized.split('\n').map(line => {
		const probe = line.trimStart();
		if ((_HEADER_RE.test(probe) || _FENCE_RE.test(probe)) && !line.startsWith(_ZWSP)) {
			return _ZWSP + line;
		}
		return line;
	}).join('\n');
}

/**
 * Write a chat-path task file so the dashboard tracks chat-originated work.
 * Called by the core agent (Claude Code) when it accepts a non-trivial task from chat.
 * Reuses the same tasks/ directory and file format as voice/Discord/Telegram paths.
 *
 * Note: access_tier is hardcoded to "owner" because chat is local to the operator.
 * Revisit if /chat ever opens to non-owner users (team/other tier).
 */
export function writeChatTask(taskDescription: string): string {
	const taskId = `task-chat-${Date.now()}`;
	const timestamp = new Date().toISOString();
	// Field order: `task:` LAST so the user-supplied multi-line body
	// can't forge header fields below it. Same shape as agent-api.py's
	// /task endpoint after PR #982; consumers (`_isVoiceTask`,
	// `parse_priority_from_text`) stop scanning at the first `task:`.
	const content = [
		`id: ${taskId}`,
		`timestamp: ${timestamp}`,
		`source: chat`,
		`interaction_type: tool_initiated`,
		`channel_id: local-chat`,
		`user_id: ${process.env.SUTANDO_DM_OWNER_ID || 'chat-local'}`,
		`access_tier: owner`,
		`priority: normal`,
		`task: ${confineUserContent(taskDescription)}`,
		'',
	].join('\n');
	// Local mode: same synchronous write as always. Relay mode: fire-and-log —
	// this function's sync contract predates the seam, and chat-task tracking
	// is best-effort bookkeeping, not the delegation critical path.
	const submitted = _delegation.submitTask(taskId, content);
	if (submitted instanceof Promise) {
		submitted.catch(e => console.error(`${ts()} [TaskBridge] chat-task relay submit failed: ${e}`));
	}
	console.log(`${ts()} [TaskBridge] Chat task: ${taskId}: ${taskDescription.slice(0, 100)}`);
	return taskId;
}

// ---------------------------------------------------------------------------
// Task status notifications — sent to the web client
// ---------------------------------------------------------------------------

let _sendTaskStatus: ((taskId: string, status: string, text: string, result?: string) => void) | null = null;
const _deliveredResults = new Set<string>();

/** Test seam: whether the drain already delivered (or pre-claimed) `file`. */
export function _isDeliveredResult(file: string): boolean {
	return _deliveredResults.has(file);
}

/** Post a room-bound voice result into its room: a `proactive-result-*` file
 *  whose first line is the `[channel: <room>]` marker the ag2space gateway's
 *  `_proactive_route` sends to that room. The `.to-ag2space` name tag is the
 *  claim grammar every bridge reads (proactive_routing.proactive_filename):
 *  only that gateway can reach a Matrix room, and an untagged name would go to
 *  whichever bridge the owner last used. The filename is claimed in
 *  `_deliveredResults` at once — it passes `_shouldFallthrough`, and without
 *  the claim the next drain tick would speak the same result a second time. */
export function forwardVoiceResultToRoom(taskId: string, result: string, room: string, nowSec = Math.floor(Date.now() / 1000)): string {
	const file = `proactive-result-${taskId}-${nowSec}.to-ag2space.txt`;
	writeFileSync(join(RESULT_DIR, file), `[channel: ${room}]\n${result}`);
	_deliveredResults.add(file);
	return file;
}

/** `[dm-only]` is detected the way every text bridge detects it
 *  (result_markers.parse_markers: anywhere in the body, case-insensitive);
 *  the drain's strip below is narrower on purpose (a standalone line only). */
export const DM_ONLY_RE = /\[dm-only\]/i;

/** Keep a room-bound voice result to the owner's DM: the same gateway-tagged
 *  proactive shape as the room post, with no `[channel:]` line and the
 *  `[dm-only]` marker restored on top, so `_proactive_route` delivers it to
 *  the owner's own room whatever else the body says. Claimed at once, for the
 *  same reason as the room shape. */
export function forwardVoiceResultToOwnerDm(taskId: string, result: string, nowSec = Math.floor(Date.now() / 1000)): string {
	const file = `proactive-result-${taskId}-${nowSec}.to-ag2space.txt`;
	writeFileSync(join(RESULT_DIR, file), `[dm-only]\n${result}`);
	_deliveredResults.add(file);
	return file;
}

/** A room-bound result that declared itself `[dm-only]` goes to the owner's
 *  DM instead of the room (CLAUDE.md "Where replies go": what the owner asked
 *  for themselves goes to the DM even by voice while docked). Owner 2026-09-18,
 *  after a "look into the mute bug" analysis landed in a customer room: "it
 *  should only send messages that are RELEVANT to that room otherwise should go
 *  to the DM". Returns the DM file, or null when the task is not room-bound or
 *  the result is not dm-only — then the caller's room leg decides. */
export function keepVoiceResultToDm(taskId: string, result: string, dmOnly: boolean, nowSec = Math.floor(Date.now() / 1000)): string | null {
	if (!dmOnly || !_voiceTaskRoom(taskId)) return null;
	const file = forwardVoiceResultToOwnerDm(taskId, result, nowSec);
	console.log(`${ts()} [TaskBridge] ${taskId} result kept to the DM ([dm-only]) via ${file}`);
	return file;
}

/** What voice hears under a result kept to the DM, outside the TASK_RESULT
 *  markers, so the model says "in your DM" rather than the docked room's
 *  "in this room". */
export const DM_ONLY_DELIVERY_NOTE = 'That result was for the owner alone: its written copy went to their DM, not the room this session is docked in. Tell them it is in their DM ("I sent it to your DM"), never "in this room".';

const DEFAULT_TASK_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes default
// Per-task pending state: submission epoch, timeout (ms), and whether to
// emit a Discord DM to the owner if this task hits its timeout. dm_on_timeout
// defaults to false (silent timeout — Susan's PR #578 contract). Voice agent
// can flip it true on critical tasks to get a fallback notification.
type PendingTask = { submittedAt: number; timeoutMs: number; dmOnTimeout: boolean; taskText: string };
const _pendingTasks = new Map<string, PendingTask>();

// Dedup window: identical task text within 2 minutes → return existing taskId.
const DEDUP_WINDOW_MS = 2 * 60 * 1000;
const normalizeTask = (t: string) => t.toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 150);

/** True if the task file (in tasks/, tasks/processed/, or tasks/archive/
 * — including month-partitioned subdirs `tasks/archive/YYYY-MM/`) is
 * voice-originated (`source: voice`, see _headerIsVoice). Used by the result watcher
 * to decide whether to forward an unsent result to Discord DM when voice is
 * offline. Returns false on missing file or parse error — bias toward not
 * forwarding to keep Susan-rejected always-DM behavior off by default for
 * non-voice tasks. */
// Cache of tasks/archive/'s month-shaped (YYYY-MM) subdirectory names,
// invalidated by the archive root's own mtime — which changes whenever an
// entry (most relevantly a new month's subdir) is added. Without this,
// _readTaskHeader's caller (the 2s-interval result watcher) re-globbed the
// whole archive root, thousands of legacy loose files included, on every
// invocation — pinning a CPU core once that directory grew large.
let _archiveMonthCache: { mtimeMs: number; dirs: string[] } | null = null;
export let _archiveScanCount = 0; // test-only: counts real readdirSync(archiveRoot) calls

function _archiveMonthDirs(archiveRoot: string): string[] {
	// Stat BEFORE readdir: a subdir created mid-scan then gets cached
	// against a stale-low mtime (extra re-scan next time, never stale).
	let mtimeMs: number;
	try {
		mtimeMs = statSync(archiveRoot).mtimeMs;
	} catch {
		return [];
	}
	if (_archiveMonthCache && _archiveMonthCache.mtimeMs === mtimeMs) {
		return _archiveMonthCache.dirs;
	}
	let dirs: string[] = [];
	try {
		_archiveScanCount++;
		// Only month-shaped names (YYYY-MM); skip stray legacy files.
		dirs = readdirSync(archiveRoot).filter((entry) => /^\d{4}-\d{2}$/.test(entry));
	} catch {}
	_archiveMonthCache = { mtimeMs, dirs };
	return dirs;
}

/** Header lines of a task, located across every archive layout. Returns null
 *  when no copy of the task survives. */
export function _readTaskHeader(taskId: string): string[] | null {
	const candidates: string[] = [
		join(TASK_DIR, `${taskId}.txt`),
		join(TASK_DIR, 'processed', `${taskId}.txt`),
		// Legacy flat-archive location — kept for any task archived before
		// the YYYY-MM partitioning (PR #591) was introduced.
		join(TASK_DIR, 'archive', `${taskId}.txt`),
	];
	// Active archive layout: tasks/archive/YYYY-MM/<taskId>.txt. Glob the
	// month subdirs rather than rebuild the YYYY-MM from the task's mtime —
	// the writer's archive month and current month can differ around month
	// boundaries.
	const archiveRoot = join(TASK_DIR, 'archive');
	if (existsSync(archiveRoot)) {
		for (const entry of _archiveMonthDirs(archiveRoot)) {
			candidates.push(join(archiveRoot, entry, `${taskId}.txt`));
		}
	}
	for (const p of candidates) {
		if (!existsSync(p)) continue;
		try {
			const body = readFileSync(p, 'utf-8');
			// Stop scanning at the first `task:` delimiter. The task-file
			// format puts `task:` last on the line preceding the user-
			// supplied multi-line task body (see agent-api.py and the
			// /meeting handler). Without this stop, a body of
			// `do thing\nchannel_id: local-voice` would forge a voice-
			// task classification — the residual half of the PR #982
			// fix Qingyun flagged. Stop-at-`task:` makes consumers
			// honor the delimiter PR #982 already established.
			const headerLines: string[] = [];
			for (const l of body.split('\n')) {
				if (l.startsWith('task:')) break;
				headerLines.push(l);
			}
			return headerLines;
		} catch {}
	}
	return null;
}

export function _isVoiceTask(taskId: string): boolean {
	const headerLines = _readTaskHeader(taskId);
	if (headerLines === null) return false;
	return _headerIsVoice(headerLines);
}

/** The voice verdict over header lines.
 *  `source: voice` is the key. A room-bound voice task carries the room id in
 *  `channel_id`, so that field no longer identifies voice; `media_form:
 *  live_stream` never does (the phone skill stamps it too). The
 *  `channel_id: local-voice` literal stays for files archived before rooms. */
function _headerIsVoice(headerLines: string[]): boolean {
	return headerLines.some(l => l.startsWith('source: voice') || l.startsWith('channel_id: local-voice'));
}

// ---------------------------------------------------------------------------
// Room-bound voice sessions. The desktop docks a voice session in a room and
// the client announces it with `session.context` frames (after session.config,
// then on every room change); while a room is bound, every task the voice
// agent delegates is addressed to that room and its result is posted there.
// ---------------------------------------------------------------------------

/** Matrix room id as the gateway's `_proactive_route` accepts it. */
export const MATRIX_ROOM_ID_RE = /^![^\s:]+:\S+$/;
/** Display names are prose from the client; capped before they reach a prompt. */
export const ROOM_NAME_MAX_CHARS = 120;

export interface VoiceSessionRoom { id: string; name?: string }

let _voiceSessionRoom: VoiceSessionRoom | null = null;
// The room released while another room's verdict is pending; change reporting still counts it as left.
let _releasedRoom: VoiceSessionRoom | null = null;

/** Bind (or, with null, release) the room the live voice session is docked in. */
export function setVoiceSessionRoom(room: VoiceSessionRoom | null): void {
	_voiceSessionRoom = room;
	_releasedRoom = null;
}

export function getVoiceSessionRoom(): VoiceSessionRoom | null {
	return _voiceSessionRoom;
}

/** The room a `session.context` frame binds, null for a DM/absent room, or
 *  undefined when `msg` is not a session.context frame at all. The type
 *  literal mirrors SESSION_CONTEXT_TYPE in web-voice-transport.ts (the client
 *  file is re-vendored into the desktop, so it cannot be imported here);
 *  tests/task-bridge-voice-room-result.test.ts pins the two together. */
export function parseSessionContextFrame(msg: Record<string, unknown> | null | undefined): VoiceSessionRoom | null | undefined {
	if (!msg || msg.type !== 'session.context') return undefined;
	const id = typeof msg.room_id === 'string' ? msg.room_id.trim() : '';
	if (!id || !MATRIX_ROOM_ID_RE.test(id)) return null;
	const rawName = typeof msg.room_name === 'string' ? msg.room_name.replace(/[\r\n]+/g, ' ').trim() : '';
	const name = rawName.slice(0, ROOM_NAME_MAX_CHARS);
	return name ? { id, name } : { id };
}

/** What a frame did to the binding: `entered` a room (from the DM or from
 *  another room), `left` for the DM, or `none` (a duplicate, or DM-to-DM). */
export type SessionRoomChange = 'none' | 'entered' | 'left';

/** Apply a `session.context` frame for the life of the session: the client
 *  sends one per room change and a DM frame when it leaves rooms, so the last
 *  frame wins and a DM frame releases the room. Tasks written afterwards use
 *  the room current at write time. Returns the change so the caller speaks a
 *  notice once per actual change and never per duplicate frame; undefined
 *  when `msg` is not a session.context frame.
 *  Trust boundary: this applies whatever room the frame names. Live frames go
 *  through bindSessionContextFrame, which admits a room only on the gateway
 *  bridge's membership verdict; call this directly only with a verified room. */
export function applySessionContextFrame(msg: Record<string, unknown> | null | undefined): { change: SessionRoomChange; room: VoiceSessionRoom | null } | undefined {
	const room = parseSessionContextFrame(msg);
	if (room === undefined) return undefined;
	return _applyVerifiedRoom(room);
}

function _applyVerifiedRoom(room: VoiceSessionRoom | null): { change: SessionRoomChange; room: VoiceSessionRoom | null } {
	const prev = _voiceSessionRoom ?? _releasedRoom;
	_voiceSessionRoom = room;
	_releasedRoom = null;
	if (room && room.id !== prev?.id) return { change: 'entered', room };
	if (!room && prev) return { change: 'left', room: null };
	return { change: 'none', room };
}

// ---------------------------------------------------------------------------
// Membership verdicts. The client's room id is a claim, not a fact: a modified
// client can name any well-formed room. Only the gateway bridge holds the
// credentials to prove the agent AND its owner are joined there, so it answers
// `state/voice-room-checks/<key>.request.json` with `<key>.verdict.json`
// (src/voice_room_membership.py) and this side binds, stamps and routes on
// nothing but that verdict. No verdict, a stale one, or a refusal all read as
// "not a room": the session stays on the DM.
// ---------------------------------------------------------------------------

export const VOICE_ROOM_CHECK_DIR = join(STATE_DIR, 'voice-room-checks');
/** Mirrors VERDICT_TTL_S in voice_room_membership.py. */
export const VOICE_ROOM_VERDICT_TTL_S = 60;
export const VOICE_ROOM_VERDICT_TIMEOUT_MS = 6000;

export interface VoiceRoomVerdict {
	room_id: string;
	verified: boolean;
	reason: string;
	checked_at: number;
	agent_joined?: boolean;
	owner_joined?: boolean;
}

export type VoiceRoomVerifier = (room: string) => Promise<VoiceRoomVerdict>;

/** Filesystem-safe key for a room id: readable prefix plus a hash so two ids
 *  that flatten alike never share a verdict file. */
export function voiceRoomCheckKey(room: string): string {
	const flat = room.replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 80);
	return `${flat}-${createHash('sha256').update(room).digest('hex').slice(0, 16)}`;
}

/** The verifier's answer for `room` while it is fresh, else null. A verdict
 *  naming another room (a key collision or a tampered file) is null too. */
export function readVoiceRoomVerdict(room: string, nowSec = Date.now() / 1000): VoiceRoomVerdict | null {
	try {
		const raw = JSON.parse(readFileSync(join(VOICE_ROOM_CHECK_DIR, `${voiceRoomCheckKey(room)}.verdict.json`), 'utf-8'));
		if (!raw || raw.room_id !== room || typeof raw.checked_at !== 'number') return null;
		if (nowSec - raw.checked_at > VOICE_ROOM_VERDICT_TTL_S || raw.checked_at - nowSec > 5) return null;
		return {
			room_id: room, verified: raw.verified === true, reason: typeof raw.reason === 'string' ? raw.reason : '',
			checked_at: raw.checked_at, agent_joined: raw.agent_joined === true, owner_joined: raw.owner_joined === true,
		};
	} catch {
		return null;
	}
}

const _unverified = (room: string, reason: string): VoiceRoomVerdict =>
	({ room_id: room, verified: false, reason, checked_at: Date.now() / 1000 });

/** Ask the gateway bridge whether owner and agent are joined in `room`. A
 *  fresh verdict on disk answers at once; otherwise one request is written and
 *  the answer awaited up to `timeoutMs`. Silence (no bridge, no gateway) is a
 *  refusal: a room nobody can vouch for is not bound. */
export async function requestVoiceRoomVerdict(room: string, opts: { timeoutMs?: number; pollMs?: number } = {}): Promise<VoiceRoomVerdict> {
	if (!MATRIX_ROOM_ID_RE.test(room)) return _unverified(room, 'not a matrix room id');
	const cached = readVoiceRoomVerdict(room);
	if (cached) return cached;
	const timeoutMs = opts.timeoutMs ?? VOICE_ROOM_VERDICT_TIMEOUT_MS;
	const pollMs = opts.pollMs ?? 100;
	try {
		mkdirSync(VOICE_ROOM_CHECK_DIR, { recursive: true });
		const key = voiceRoomCheckKey(room);
		const tmp = join(VOICE_ROOM_CHECK_DIR, `${key}.request.json.tmp`);
		writeFileSync(tmp, JSON.stringify({ room_id: room, requested_at: Date.now() / 1000 }));
		renameSync(tmp, join(VOICE_ROOM_CHECK_DIR, `${key}.request.json`));
	} catch (e) {
		return _unverified(room, `request not written: ${e instanceof Error ? e.message : String(e)}`);
	}
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		await new Promise(r => setTimeout(r, pollMs));
		const verdict = readVoiceRoomVerdict(room);
		if (verdict) return verdict;
	}
	return _unverified(room, 'no verdict from the gateway bridge');
}

let _voiceRoomVerifier: VoiceRoomVerifier = requestVoiceRoomVerdict;

/** Test seam: replace (or with null restore) the membership verifier. */
export function setVoiceRoomVerifier(fn: VoiceRoomVerifier | null): void {
	_voiceRoomVerifier = fn ?? requestVoiceRoomVerdict;
}

export interface SessionRoomBinding {
	change: SessionRoomChange;
	room: VoiceSessionRoom | null;
	/** Set when the frame named a room the gateway bridge would not vouch for. */
	refused?: { id: string; reason: string };
}

let _sessionContextSeq = 0;

/** The live-frame entry point: a DM frame applies at once; a room frame
 *  applies only after the verifier confirms membership, and until then (or on
 *  a refusal) the session is on the DM: a different room bound before the
 *  frame is released before the wait. A newer frame that arrives while a
 *  verdict is pending wins; the older one then returns `change: 'none'`. */
export async function bindSessionContextFrame(msg: Record<string, unknown> | null | undefined): Promise<SessionRoomBinding | undefined> {
	const room = parseSessionContextFrame(msg);
	if (room === undefined) return undefined;
	const seq = ++_sessionContextSeq;
	if (!room) return _applyVerifiedRoom(null);
	if (_voiceSessionRoom && _voiceSessionRoom.id !== room.id) {
		_releasedRoom = _voiceSessionRoom;
		_voiceSessionRoom = null;
	}
	let verdict: VoiceRoomVerdict;
	try {
		verdict = await _voiceRoomVerifier(room.id);
	} catch (e) {
		verdict = _unverified(room.id, `verifier failed: ${e instanceof Error ? e.message : String(e)}`);
	}
	if (seq !== _sessionContextSeq) return { change: 'none', room: _voiceSessionRoom };
	if (verdict.verified) return _applyVerifiedRoom(room);
	console.log(`${ts()} [SessionRoom] refused ${room.id}: ${verdict.reason} — session stays on the DM`);
	return { ..._applyVerifiedRoom(null), refused: { id: room.id, reason: verdict.reason } };
}

/** The room a finished voice task may answer in: its header's room, re-checked
 *  against a current verdict (membership can change while the task runs).
 *  Null sends the result the DM way. */
export async function resolveVoiceResultRoom(taskId: string): Promise<string | null> {
	const room = _voiceTaskRoom(taskId);
	if (!room) return null;
	let verdict: VoiceRoomVerdict;
	try {
		verdict = await _voiceRoomVerifier(room);
	} catch (e) {
		verdict = _unverified(room, `verifier failed: ${e instanceof Error ? e.message : String(e)}`);
	}
	if (verdict.verified) return room;
	console.log(`${ts()} [TaskBridge] ${taskId} result not posted to ${room}: ${verdict.reason} — delivered the DM way`);
	return null;
}

/** Voice result with no client attached: into its verified room — unless it
 *  is `[dm-only]`, then the owner's DM — else the owner-DM proactive shape
 *  every bridge already delivers. */
export async function forwardOfflineVoiceResult(taskId: string, result: string, nowSec = Math.floor(Date.now() / 1000), dmOnly = false): Promise<string> {
	const kept = keepVoiceResultToDm(taskId, result, dmOnly, nowSec);
	if (kept) return kept;
	const room = await resolveVoiceResultRoom(taskId);
	if (room) {
		const file = forwardVoiceResultToRoom(taskId, result, room, nowSec);
		console.log(`${ts()} [TaskBridge] Voice offline; forwarded ${taskId} result to room ${room} via ${file}`);
		return file;
	}
	const file = `proactive-result-${taskId}-${nowSec}.txt`;
	writeFileSync(join(RESULT_DIR, file), result);
	_deliveredResults.add(file);
	console.log(`${ts()} [TaskBridge] Voice offline; forwarded ${taskId} result to the owner DM via ${file}`);
	return file;
}

/** The one system line the model hears when the docked room changes; null
 *  when nothing changed. Pure: the caller frames and injects it. */
export function sessionRoomNotice(change: SessionRoomChange, room: VoiceSessionRoom | null): string | null {
	if (change === 'entered' && room) {
		const label = room.name ? `"${room.name}"` : room.id;
		return `You are docked in room ${label}; what you delegate is answered there only when it is for the room's members, otherwise in the owner's DM; say where it went. No reply is needed.`;
	}
	if (change === 'left') {
		return 'You are back in your DM. Work you delegate answers there; say "in your DM" or "here", never "in this room". No reply is needed.';
	}
	return null;
}

/** Every header line of a voice task, above `task:`. One writer for the work
 *  tool and the cancel tool so the two cannot drift. A room-bound task keeps
 *  `source: voice` and `media_form: live_stream` as its voice identity and
 *  addresses the room through `channel_id`, `channel_kind` and
 *  `source_room_id` — the same keys a gateway-written room task carries, so
 *  the core's "reply where you were asked" rule applies unchanged. */
export function buildVoiceTaskHeader(taskId: string, timestamp: string, ownerId: string, room: string | null): string {
	const lines = [
		`id: ${taskId}`,
		`timestamp: ${timestamp}`,
		`source: voice`,
		`interaction_type: realtime_audio`,
		// interaction-model 4D, step 1.5 (scope A): the media-form axis on
		// live-plane tasks. `live_stream` = the payload originates from a
		// continuous real-time session (frames stay out-of-band; provenance).
		'media_form: live_stream',
		`channel_id: ${room ?? 'local-voice'}`,
	];
	if (room) lines.push('channel_kind: room', `source_room_id: ${room}`);
	lines.push(`user_id: ${ownerId}`, 'access_tier: owner', 'priority: urgent');
	return lines.join('\n') + '\n';
}

/** The one body line under `task:` that tells the core how to answer a task
 *  delegated while docked in a room. A body line, not a header key: the core
 *  reads it as guidance, no consumer parses it, and it needs no
 *  KNOWN_HEADER_KEYS entry. The drain honours the marker it names. */
export function voiceRoomTaskGuidance(room: string): string {
	return `room_context: ${room} — post there only what its members are meant to read; for anything the owner asked for themselves start the result with [dm-only]`;
}

/** The room a voice task was delegated from, read through the same
 *  delimiter-honoring header reader as `_isVoiceTask`; null for a DM voice
 *  task, a non-voice task, a missing file or a malformed room id. */
export function _voiceTaskRoom(taskId: string): string | null {
	const headerLines = _readTaskHeader(taskId);
	if (headerLines === null || !_headerIsVoice(headerLines)) return null;
	const line = headerLines.find(l => l.startsWith('source_room_id:'));
	const room = line ? line.slice('source_room_id:'.length).trim() : '';
	return MATRIX_ROOM_ID_RE.test(room) ? room : null;
}

const CLAIM_LEDGERS = 'remote-task-inflight';

// Durable in-flight sets other consumers publish. Cached on (mtime, size) so a
// drain does not re-parse per result; a claim added mid-drain is picked up on
// the next change rather than needing a restart.
let _ledgerCache: { key: string; ids: Set<string> } | null = null;

export function _claimedElsewhere(taskId: string): boolean {
	// Defence in depth: this runs inside the result-drain loop, where a throw
	// aborts the pass for every later-sorting file without logging.
	try {
		// ONLY this workspace's state dir. A consumer configured against another
		// tree writes its results there too, so its claims describe files that
		// are not in the results/ being scanned here — reading them could only
		// mistake a foreign namespace's claim for ownership of this file.
		const dir = join(REPO_DIR, 'state');
		let names: string[];
		try {
			names = readdirSync(dir).filter(f => f.startsWith(CLAIM_LEDGERS) && f.endsWith('.json')).sort();
		} catch { return false; }
		const stamps: string[] = [];
		for (const f of names) {
			try { const st = statSync(join(dir, f)); stamps.push(`${f}:${st.mtimeMs}:${st.size}`); } catch {}
		}
		const key = stamps.join('|');
		if (_ledgerCache?.key !== key) {
			const ids = new Set<string>();
			for (const f of names) {
				try {
					const parsed = JSON.parse(readFileSync(join(dir, f), 'utf-8'));
					// An unreadable or reshaped ledger yields no claims rather than
					// throwing; the source-label net still covers those consumers.
					if (Array.isArray(parsed)) for (const id of parsed) if (typeof id === 'string') ids.add(id);
				} catch {}
			}
			_ledgerCache = { key, ids };
		}
		return _ledgerCache.ids.has(taskId);
	} catch { return false; }
}

/** Origin of a task for the retirement decision, read through the same
 *  delimiter-honoring header reader `_isVoiceTask` uses. */
export function _taskOrigin(taskId: string): TaskOrigin | null {
	const headerLines = _readTaskHeader(taskId);
	if (headerLines === null) return null;
	const line = headerLines.find(l => l.startsWith('source:'));
	return {
		source: line ? line.slice('source:'.length).trim() : null,
		claimedElsewhere: _claimedElsewhere(taskId),
	};
}

/** Id prefix minted by `submit_signal_room_task` (src/signal_room_tasks.py).
 * Task-bridge delivers NO Signal Room result: the room daemon polls agent-api
 * `GET /result/{id}` for its own. Kept in sync with the Python writer. */
export const SIGNAL_TASK_PREFIX = 'task-signal-';

/** Belt-suspenders guard for the result-watcher's unconditional fallthrough
 * (issue #1035, follow-up to PR #1033). Returns true iff the filename is one
 * that task-bridge legitimately delivers via `onResult()`. Rejects everything
 * else — most importantly, the new `<channel-key>.task-{id}.txt` namespace
 * PR #1033 introduced for the per-channel pull path (phone / plugin surfaces),
 * which the per-channel scanner consumes itself.
 *
 * `proactive-*` IS allowed: per the long-standing proactive-voice rule,
 * proactive messages are spoken by the voice agent when the client is
 * connected (in parallel to discord-bridge's poll_proactive DM-delivery).
 * That delivery has no explicit handler upstream in this watcher — the
 * fallthrough IS the path — so blocking `proactive-*` here would silently
 * disable voice-spoken proactive messages.
 *
 * Exported for unit testing — the watcher's setInterval body is otherwise
 * awkward to exercise in isolation. */
export function _shouldFallthrough(file: string): boolean {
	// Signal Room results belong to the room daemon's `/result` poll, not to
	// voice. See SIGNAL_TASK_PREFIX and the dedicated branch in the watcher.
	if (file.startsWith(SIGNAL_TASK_PREFIX)) return false;
	return file.startsWith('task-') || file.startsWith('voice-') || file.startsWith('proactive-');
}



/**
 * Whether a result file should REGISTER a row in the Task list — i.e. fire
 * `_sendTaskStatus` (live web-socket task card) and POST `/task-done`
 * (agent-api `task_history`). Only genuine `task-*.txt` results are tasks.
 *
 * `proactive-*` notification files also pass `_shouldFallthrough` so they get
 * SPOKEN by the voice agent (the proactive-voice delivery path), but they are
 * NOT tasks: registering them keys a `task_history` row by the file stem, so
 * every re-fire of a proactive notification (e.g. the hourly pending-question
 * reminder) lands a fresh `proactive-<ts>` id as a DUPLICATE Task row. This is
 * the general fix for that class (#1786); the narrow #1784 only stabilized the
 * pending-question filename so its duplicate rows collapsed to one. `voice-*`
 * files are short-circuited earlier in the watcher and never reach this path.
 *
 * Exported for unit testing alongside `_shouldFallthrough`. */
export function _shouldRegisterTaskRow(file: string): boolean {
	return file.startsWith('task-');
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

// The core's own bookkeeping files are not the owner's queue. Mirrors
// src/task_queue.py BOOKKEEPING_PREFIXES, the pending list's single owner.
const QUEUE_BOOKKEEPING_PREFIXES = ['task-cron-', 'task-bench-', 'task-workstream-', 'task-project-grouping-'];

/** How many owner tasks are pending in `dir` besides `excludeId`: the voice
 *  agent's "N ahead of this one". A directory it cannot read counts as 0 —
 *  the number is a courtesy line, never a reason to fail the delegation. */
export function countQueuedAhead(dir: string, excludeId: string): number {
	let names: string[];
	try { names = readdirSync(dir); } catch { return 0; }
	return names.filter(f => f.startsWith('task-') && f.endsWith('.txt') && f !== `${excludeId}.txt`
		&& !QUEUE_BOOKKEEPING_PREFIXES.some(p => f.startsWith(p))).length;
}

/** The sentence the voice agent says when other tasks are ahead; empty when none are. */
export function queuedAheadInstruction(queuedAhead: number): string {
	if (queuedAhead <= 0) return '';
	const line = queuedAhead === 1
		? 'Got it, right after the one I\'m on.'
		: `Got it, ${queuedAhead} in line before this one.`;
	return ` ${queuedAhead} task(s) are still running ahead of this one. Tell the user exactly "${line}" and wait; do not narrate the queue again.`;
}

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
		// Skipped on Windows: shells out to /bin/sh + bash + invokes a .sh skill
		// that isn't ported yet. The slow file-bridge path below still works.
		const concatMatch = /\b(prepend|concatenat|concat|image.*video|video.*image)\b/i.test(task);
		if (concatMatch && process.platform !== 'win32') {
			try {
				const { execFileSync } = await import('node:child_process');
				// ls globs need shell for wildcard expansion — command strings are static literals (fixes #1451)
				const image = execFileSync('/bin/sh', ['-c', 'ls -t /tmp/discord-inbox/*.jpg /tmp/discord-inbox/*.png 2>/dev/null | head -1'], { timeout: 3000 }).toString().trim();
				const video = execFileSync('/bin/sh', ['-c', 'ls -t /tmp/sutando-recording-*-narrated-subtitled.mov /tmp/sutando-recording-*-narrated.mov /tmp/sutando-recording-*.mov 2>/dev/null | head -1'], { timeout: 3000 }).toString().trim();
				if (image && video) {
					// execFileSync argv array bypasses shell — image/video paths are separate args, no interpolation (fixes #1451)
					const scriptPath = resolve(claudeHomePath('skills', 'video-concat', 'scripts', 'prepend-image.sh'));
					const result = execFileSync('bash', [scriptPath, image, video, '3'], { timeout: 60000 }).toString().trim();
					const parsed = JSON.parse(result);
					return { status: 'done', result: `Video with image prepended: ${parsed.output} (${parsed.size_mb}MB)` };
				}
			} catch (e) { console.log(`${ts()} [TaskBridge] fast path concat failed: ${e}`); }
		}

		// Check if the watcher (Claude Code brain) is running. The historic probe
		// uses `pgrep -f watch-tasks` (POSIX only). On Windows we fall back to a
		// PID-file sentinel written by src/watch-tasks-stream.ps1.
		let watcherOnline = false;
		try {
			if (process.platform === 'win32') {
				const { existsSync, readFileSync } = await import('node:fs');
				const pidFile = join(REPO_DIR, 'state', 'watch-tasks-stream.pid');
				if (existsSync(pidFile)) {
					const pid = parseInt(readFileSync(pidFile, 'utf-8').trim());
					if (pid > 0) {
						try {
							// `process.kill(pid, 0)` is a liveness probe (signal 0); throws if process is gone.
							process.kill(pid, 0);
							watcherOnline = true;
						} catch {}
					}
				}
			} else {
				const { execFileSync } = await import('node:child_process');
				// execFileSync argv array — no shell interpolation (fixes #1451)
				const watcherRunning = execFileSync('pgrep', ['-f', 'watch-tasks'], { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'ignore'] }).trim();
				watcherOnline = !!watcherRunning;
			}
		} catch {
			// pgrep returns exit code 1 if no match
		}
		if (!watcherOnline) {
			console.log(`${ts()} [TaskBridge] WARNING: watcher offline — task will be queued for next cron pass`);
		}

		// Dedup: if the same task text is already pending (within DEDUP_WINDOW_MS),
		// return the existing taskId instead of writing a duplicate task file.
		const normalizedTask = normalizeTask(task);
		const now = Date.now();
		for (const [existingId, pending] of _pendingTasks) {
			if (
				normalizeTask(pending.taskText) === normalizedTask &&
				now - pending.submittedAt < DEDUP_WINDOW_MS
			) {
				console.log(`${ts()} [TaskBridge] Dedup: task matches ${existingId} (submitted ${Math.round((now - pending.submittedAt) / 1000)}s ago)`);
				return {
					status: 'duplicate',
					taskId: existingId,
					message: `Task already pending as ${existingId}. Do NOT submit again — tell the user you are already working on it.`,
				};
			}
		}

		const taskId = `task-${Date.now()}`;
		const timestamp = new Date().toISOString();
		const ownerId = process.env.SUTANDO_DM_OWNER_ID || 'voice-local';
		// Field order: `task:` LAST so the user-supplied (Gemini-relayed,
		// possibly multi-line) task body can't forge header fields. Same
		// shape as agent-api.py's /task endpoint after PR #982; consumers
		// (`_isVoiceTask`, `parse_priority_from_text`) stop scanning at
		// the first `task:` line.
		// Attach a short window of recent conversation AFTER the `task:` line so
		// the core can self-correct a misheard/garbled transcript (per Chi: "the
		// voice agent may mishear and pass the wrong transcripts"). It lands in
		// the task BODY (everything after `task:`), so it cannot forge header
		// fields — consumers stop scanning headers at the first `task:` line.
		// Best-effort: empty string if no log/session yet.
		let contextBlock = '';
		try {
			const recent = getRecentConversation(4);
			if (recent) {
				contextBlock =
					`\n\n--- recent voice transcript (may contain ASR errors; if the task above ` +
					`seems garbled or doesn't match this, infer the true intent from it or ask to ` +
					`confirm before acting) ---\n${confineUserContent(recent)}\n`;
			}
		} catch { /* best effort — never block delegation on context attach */ }
		// A session docked in a room addresses the task to that room (headers
		// via buildVoiceTaskHeader) and tells the core, in the body, what that
		// room may read; a DM session keeps `channel_id: local-voice`.
		const room = _voiceSessionRoom?.id ?? null;
		const roomGuidance = room ? `\n${voiceRoomTaskGuidance(room)}` : '';
		const content =
			buildVoiceTaskHeader(taskId, timestamp, ownerId, room) +
			`task: ${confineUserContent(task)}${roomGuidance}${contextBlock}\n`;
		await _delegation.submitTask(taskId, content);
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
		_pendingTasks.set(taskId, { submittedAt: Date.now(), timeoutMs, dmOnTimeout: dm_on_timeout === true, taskText: task });
		// Record owner activity for status-aware-pivot in proactive loop
		writeOwnerActivity('voice', task);
		console.log(`${ts()} [TaskBridge] Task ${taskId}: ${task.slice(0, 100)}`);
		_sendTaskStatus?.(taskId, 'working', task.slice(0, 60));
		// Counted after the write, so the file just written is excluded by id and
		// everything older in tasks/ is what stands ahead of it.
		const queuedAhead = countQueuedAhead(TASK_DIR, taskId);
		return {
			status: 'pending',
			taskId,
			queuedAhead,
			message: (watcherOnline
				? 'Task has been queued and is being processed. The result will be spoken when ready. Do NOT tell the user the task is done — say you are working on it.'
				: 'Task has been saved. The processing engine will pick it up on its next pass (within a few minutes). Tell the user the task is queued and will be handled shortly.')
				+ queuedAheadInstruction(queuedAhead),
		};
	},
};

// cancelTask tool moved — canonical version is `cancelTaskTool` in inline-tools.ts.

// ---------------------------------------------------------------------------
// Result watcher — call this once at startup to watch for results
// and inject them into the conversation via a callback
// ---------------------------------------------------------------------------

/** Append a message to the persistent conversation log. The text cap
 *  matches Discord's per-message limit (2000 chars) so a single transcript
 *  line never exceeds what could legitimately appear elsewhere in the
 *  conversation. The previous 200-char cap was aggressive — it truncated
 *  ordinary user/assistant turns mid-sentence, especially in CJK where one
 *  character can render as multiple bytes. Lifted to LOG_LINE_MAX_CHARS;
 *  override via SUTANDO_LOG_LINE_MAX_CHARS env if a host wants tighter logs. */
const LOG_LINE_MAX_CHARS = Number(process.env.SUTANDO_LOG_LINE_MAX_CHARS) || 2000;
export function logConversation(role: string, text: string, sessionId?: string): void {
	const capped = text.replace(/\n/g, ' ').slice(0, LOG_LINE_MAX_CHARS);
	const line = `${new Date().toISOString()}|${role}|${capped}\n`;
	try { appendFileSync(CONVERSATION_LOG, line); } catch { /* best effort */ }
	recordConversation(role, capped, sessionId); // #603 sqlite mirror — best-effort, swallowed inside
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
	recordSessionBoundary(reason); // #603 sqlite mirror
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
	// Reads the text conversation.log directly: it is the primary truth for
	// per-turn content. The sqlite mirror is a best-effort parallel write
	// (errors swallowed in conversation-store.ts) so it can silently lag the
	// log — trusting it here could return a stale "last turn". The current
	// session is small, so the backward walk is cheap regardless.
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
	// Reads the text conversation.log directly — primary truth for per-turn
	// content. The sqlite mirror is best-effort and may lag; trusting it
	// could replay a stale window. Current-session window is small.
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
const NOTE_VIEWING_FILE = join(tmpdir(), 'sutando-note-viewing.json');

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
					// `task:` last so the (multi-line) context-drop body can't
					// forge header fields. Same shape as the voice/chat task
					// writers and agent-api.py's /task endpoint per PR #982.
					const taskContent =
						`id: ${taskId}\n` +
						`timestamp: ${new Date().toISOString()}\n` +
						`source: context-drop\n` +
						`interaction_type: system_event\n` +
						`channel_id: local-hotkey\n` +
						`user_id: ${ownerId}\n` +
						`access_tier: owner\n` +
						`priority: normal\n` +
						`task: User dropped context via hotkey. Process this:\n${confineUserContent(content)}\n`;
					const stampedContent = tryStampText(taskContent);
					writeFileSync(
						join(TASK_DIR, `${taskId}.txt`),
						stampedContent,
					);
					emitTaskProcessed(stampedContent);
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

/** Split-host result loop (relay mode only). Scope is deliberately the
 * delegation-critical subset of the local watcher: timeout expiry for
 * _pendingTasks, and delivery+archive of results for tasks THIS process
 * submitted. Results it doesn't own are left untouched for their real
 * consumers on the core host. Skip markers get the same silent-archive
 * treatment as the local path. */
function startRelayResultWatcher(onResult: ResultListener): void {
	console.log(`${ts()} [TaskBridge] Relay result watcher polling core agent-api`);
	let inFlight = false;
	setInterval(async () => {
		if (inFlight) return; // don't stack slow HTTP polls
		inFlight = true;
		try {
			// Timeout sweep — same semantics as the local watcher, minus the
			// core-host file ops (task archival happens core-side).
			for (const [taskId, pending] of _pendingTasks) {
				const { submittedAt, timeoutMs } = pending;
				if (timeoutMs === 0) continue;
				if (Date.now() - submittedAt > timeoutMs) {
					_pendingTasks.delete(taskId);
					const minutes = Math.floor(timeoutMs / 60000);
					const snippet = pending.taskText.length > 80 ? pending.taskText.slice(0, 77) + '...' : pending.taskText;
					_sendTaskStatus?.(taskId, 'timeout', `Task '${snippet}' timed out — core agent may be unresponsive`);
					onResult(`[Task ${taskId} ('${snippet}') timed out after ${minutes} minutes. The core host may be unreachable or busy.]`);
				}
			}
			const files = await _delegation.listResultFiles();
			for (const file of files) {
				if (_deliveredResults.has(file)) continue;
				const taskId = file.replace('.txt', '');
				if (!_pendingTasks.has(taskId)) continue; // not ours — leave it
				const result = (await _delegation.readResultFile(file)).trim();
				if (!result) continue;
				_deliveredResults.add(file);
				_pendingTasks.delete(taskId);
				// Shared predicate, not a local regex: this grammar must stay identical to
				// src/result_markers.py, which is case-insensitive and accepts `[deduped:]`.
				if (!bodyIsSkipMarked(result)) {
					_sendTaskStatus?.(taskId, 'done', 'Task complete', result);
					onResult(`[Task result for ${taskId}]\n${result}`);
				}
				await _delegation.archiveResultFile(file, taskId);
			}
		} catch (err) {
			// Transient network failure: keep polling — the core keeps results
			// durable in results/ until archived, so nothing is lost.
			console.error(`${ts()} [TaskBridge] relay result poll failed (will retry):`, err);
		} finally {
			inFlight = false;
		}
	}, 2000);
}

/** The drain's listener: the result text, plus an optional delivery note the
 *  adapter injects under it (where the written copy went) when it differs from
 *  what the session was told to expect. */
export type ResultListener = (result: string, deliveryNote?: string) => void;

export function startResultWatcher(onResult: ResultListener, isClientConnected: () => boolean): void {
	if (_delegation.mode === 'relay') {
		// Split-host mode: the local watcher below reads core-host state
		// (task files for timeout snippets, voice-/question-/proactive- flows,
		// context consolidation) that doesn't exist on this machine. The relay
		// watcher covers the delegation-critical subset — results for tasks
		// THIS process submitted — and nothing else: consuming other task-*
		// results here would steal them from their real consumers on the core
		// host (deliver-once). Core-host-only flows stay core-host-only.
		startRelayResultWatcher(onResult);
		return;
	}
	console.log(`${ts()} [TaskBridge] Watching for results in ${RESULT_DIR}`);

	// Check every 2 seconds for new result files
	setInterval(() => {
		// Defensive try/catch around the timeout-check loop. Without this,
		// a single throw during _pendingTasks iteration (corrupt entry,
		// race with concurrent delete/set, unhandled rejection in a
		// destructure of `pending`) takes down the visible behavior of
		// this tick — the readdir block below has its own try/catch, but
		// the loop above did not. Observed live 2026-05-16: post-restart
		// voice-agent's result-watcher fell silent (no TaskBridge log
		// lines across 30+ minutes) while the 30s health monitor's
		// setInterval kept firing normally — same Node process, same
		// event loop, so the only differential was an early throw in
		// this body that propagated past the setInterval callback.
		try {
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
				// Use archiveFile() — same destination (tasks/archive/<YYYY-MM>/) as
				// the result-delivery archival paths so all timeout/done/dedupe lands
				// in one place. (Mini's #589 review flagged the previous
				// tasks/processed/ split as a learn-collector scan footprint.)
				if (existsSync(taskFile)) {
					archiveFile(taskFile, 'tasks', taskId);
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
		} catch (err) {
			console.error(`${ts()} [TaskBridge] timeout-check loop threw (non-fatal, continuing watch):`, err);
		}

		try {
			const files = readdirSync(RESULT_DIR).filter(f => f.endsWith('.txt')).sort();
			if (files.length === 0) return;

			const clientConnected = isClientConnected();

			for (const file of files) {
				if (_deliveredResults.has(file)) continue;
				const path = join(RESULT_DIR, file);
				// `[dm-only]` is a Discord-routing privacy marker (see
				// src/result_markers.py) — on the Python bridge side it suppresses
				// any [channel:] redirect on the same body (so a body carrying
				// private data can't be redirected out to a shared channel). It does
				// NOT by itself force DM delivery — routing to the owner's DM stays
				// the consumer's job (for a proactive-* result the default
				// destination already is the owner's DM). It has no meaning for the
				// voice/task path, so strip it on read: this keeps voice from ever
				// speaking "dm only" and keeps it out of logs. Parity with Python
				// parse_markers(), which strips ONLY a STANDALONE marker — one alone
				// on its line. An inline mention is prose (a result DISCUSSING the
				// marker) and rewriting it silently corrupts owner-facing text:
				//   in  "- #2170 [dm-only]: closes the leak vector"
				//   out "- #2170 : closes the leak vector"
				// The old expression here was /\[dm-only\]\s*/gi, which stripped
				// every occurrence and made this consumer disagree with every
				// text bridge after the Python side was narrowed.
				const rawResult = readFileSync(path, 'utf-8');
				// Detected before the strip: a room-bound voice result that carries
				// the marker is kept to the owner's DM (keepVoiceResultToDm).
				const dmOnly = DM_ONLY_RE.test(rawResult);
				const result = rawResult
					.replace(/^[ \t]*\[dm-only\][ \t]*\r?\n?/gim, '')
					.trim();
				if (!result) continue;
				const taskId = file.replace('.txt', '');

				// Voice-only push channel: files named `voice-*.txt` are spoken
				// by the voice agent on next turn, OR held in queue until the
				// voice client reconnects. Discord-bridge skips them. Use this
				// when the content is meaningless on Discord (e.g. a draft
				// meant for voice to TYPE into a text field). Per Chi's
				// 2026-05-20 02:25 ask after the proactive-* race.
				if (file.startsWith('voice-')) {
					if (!clientConnected) {
						// Hold in queue; don't archive. Next poll will try again.
						continue;
					}
					console.log(`${ts()} [TaskBridge] Voice-only result: ${file} (${result.slice(0, 80)})`);
					onResult(result);
					_deliveredResults.add(file);
					setTimeout(() => archiveFile(path, 'results', `voice-${Date.now()}`), 10_000);
					continue;
				}
				// [no-send] / [REPLIED] / [deduped: <id>] — archive silently, no voice.
				// deduped had its own branch above this one, bypassing the ownership gate.
				// These are set by the core agent when delivery already happened via another path
				// (e.g. Discord bridge already replied) or the result should be suppressed entirely.
				// Parity with Python bridges: discord-bridge.py and telegram-bridge.py both honor
				// these via parse_markers(); task-bridge.ts must too (issue #1381).
				if (isSkipMarked(file, result)) {
					// Ownership must survive a restart (_pendingTasks is in-memory)
					// and the timeout sweep; suppression applies either way.
					const owns = (id: string) => _pendingTasks.has(id) || _isVoiceTask(id);
					if (!mayRetireSkipMarked(file, result, owns, _taskOrigin)) {
						continue;   // another consumer's: leave the files for its owner
					}
					console.log(`${ts()} [TaskBridge] ${taskId} has skip marker; archiving silently`);
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
				// Signal Room: the room daemon polls agent-api `GET /result/{id}`, so
				// task-bridge owns no delivery here. Falling through would speak
				// untrusted room speech into the owner's private call, and the
				// `foreignOrigin` path below would leave the files for a bridge that
				// does not exist. Register the owner-visible Task row, then archive —
				// `/result` falls back to find_archived_result, so a later poll by the
				// daemon still finds the body.
				if (taskId.startsWith(SIGNAL_TASK_PREFIX)) {
					console.log(`${ts()} [TaskBridge] ${taskId} is a Signal Room task; room daemon polls /result — archiving without voice`);
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
					}, 10_000);
					continue;
				}
				// Voice client offline → forward voice-task results to Discord DM
				// via a proactive-result-*.txt file (poll_proactive in
				// discord-bridge.py picks it up and DMs the owner). Skips files
				// that aren't voice-originated tasks (Discord/Telegram bridges
				// handle their own deliveries via pending_replies).
				if (!clientConnected) {
					if (file.startsWith('task-') && _isVoiceTask(taskId)) {
						// Claimed now, delivered once the room verdict is in: a task
						// delegated from a room answers there only while the gateway
						// bridge still vouches for it, else the owner DM gets it.
						_deliveredResults.add(file);
						_pendingTasks.delete(taskId);
						forwardOfflineVoiceResult(taskId, result, undefined, dmOnly).catch(e => {
							_deliveredResults.delete(file);
							console.error(`${ts()} [TaskBridge] Failed to forward ${taskId} offline:`, e);
						});
						setTimeout(() => {
							archiveFile(path, 'results', taskId);
							const taskFile = join(TASK_DIR, `${taskId}.txt`);
							if (existsSync(taskFile)) archiveFile(taskFile, 'tasks', taskId);
						}, 10_000);
					}
					// Chat-path tasks have no bridge consumer — archive them directly
					// so results/task-chat-*.txt files don't accumulate forever.
					if (taskId.startsWith('task-chat-')) {
						_sendTaskStatus?.(taskId, 'done', result.slice(0, 60), result);
						_deliveredResults.add(file);
						_pendingTasks.delete(taskId);
						console.log(`${ts()} [TaskBridge] Chat task archived (no client): ${taskId}`);
						setTimeout(() => {
							archiveFile(path, 'results', taskId);
							const taskFile = join(TASK_DIR, `${taskId}.txt`);
							if (existsSync(taskFile)) archiveFile(taskFile, 'tasks', taskId);
						}, 10_000);
					}
					// Context-drop tasks: Sutando.app writes `source: context-drop` (no bridge
					// handles delivery). Archive them directly so they don't pile up indefinitely.
					// Companion fix: Sutando.app main.swift writeTask() must include this field.
					// Issue: https://github.com/sonichi/sutando/issues/969
					if (taskId.startsWith('task-')) {
						const ctxTaskFile = join(TASK_DIR, `${taskId}.txt`);
						if (existsSync(ctxTaskFile)) {
							try {
								const taskBody = readFileSync(ctxTaskFile, 'utf-8');
								if (/^source:\s*context-drop/m.test(taskBody)) {
									_sendTaskStatus?.(taskId, 'done', result.slice(0, 60), result);
									_deliveredResults.add(file);
									_pendingTasks.delete(taskId);
									console.log(`${ts()} [TaskBridge] Context-drop task archived (no client): ${taskId}`);
									setTimeout(() => {
										archiveFile(path, 'results', taskId);
										if (existsSync(ctxTaskFile)) archiveFile(ctxTaskFile, 'tasks', taskId);
									}, 10_000);
									continue;
								}
							} catch {}
						}
					}
					// Other non-voice unsent results stay queued (their bridges deliver them)
					continue;
				}
				// Belt-suspenders guard (issue #1035, follow-up to PR #1033):
				// the fallthrough below fires onResult() for any non-empty .txt
				// when the voice client is connected. PR #1033 introduced a new
				// filename namespace `<channel-key>.task-{id}.txt` for the
				// per-channel pull path used by phone / plugin surfaces — those
				// files are NOT meant for task-bridge to inject into voice.
				// PR #1033's mitigation is the per-channel scanner's
				// read-and-delete winning the race; this guard closes the
				// race by gating the fallthrough to filenames task-bridge
				// legitimately consumes. See _shouldFallthrough for the
				// allowlisted prefixes.
				if (!_shouldFallthrough(file)) continue;
				if (result) {
					console.log(`${ts()} [TaskBridge] Result ${file}: ${result.slice(0, 100)}`);
					// Delivery affinity (owner-hit 2026-07-09 05:04): a task that
					// arrived via a REMOTE bridge (gateway/discord/telegram/slack —
					// anything not voice-origin) has its result delivered BY that
					// bridge. Voice may narrate a copy for call continuity, but
					// must NOT archive the files — archiving here starved the
					// gateway and the owner's room reply silently never went out
					// ("do we have a room event?" answered on-call only). Foreign
					// results: narrate once (in-memory dedup), leave files alone.
					const foreignOrigin = file.startsWith('task-') && !taskId.startsWith('task-chat-') && !_isVoiceTask(taskId);
					// proactive-* files reach this fallthrough only to be SPOKEN
					// (onResult below). They are NOT tasks — gate the two
					// task-registration side-effects (_sendTaskStatus + POST
					// /task-done) to genuine task-*.txt results, else each
					// proactive re-fire duplicates a Task row (#1786).
					const registersTask = _shouldRegisterTaskRow(file);
					if (registersTask) _sendTaskStatus?.(taskId, 'done', result.slice(0, 60), result);
					_deliveredResults.add(file);
					_pendingTasks.delete(taskId);
					logConversation('core-agent', `[task:${taskId}] ${result.slice(0, LOG_LINE_MAX_CHARS)}`);
					// A voice task delegated from a room is spoken AND written: into
					// that room when the answer is for its members (the owner asked
					// there, and the room keeps the record), into the owner's DM when
					// the core marked it `[dm-only]` — and voice is told which.
					const roomBound = registersTask && !foreignOrigin && _voiceTaskRoom(taskId) !== null;
					const keptToDm = roomBound ? keepVoiceResultToDm(taskId, result, dmOnly) : null;
					if (keptToDm) onResult(result, DM_ONLY_DELIVERY_NOTE);
					else onResult(result);
					if (roomBound && !keptToDm) {
						resolveVoiceResultRoom(taskId).then(room => {
							if (!room) return;
							const proactiveFile = forwardVoiceResultToRoom(taskId, result, room);
							console.log(`${ts()} [TaskBridge] Posted ${taskId} result to room ${room} via ${proactiveFile}`);
						}).catch(e => console.error(`${ts()} [TaskBridge] Failed to post ${taskId} result to its room:`, e));
					}
					// Notify agent-api directly (task results only), then delete file
					if (registersTask) {
						try {
							fetch('http://localhost:7843/task-done', {
								method: 'POST',
								headers: _apiHeaders(),
								body: JSON.stringify({ taskId, result }),
							}).catch(() => {});
						} catch {}
					}
					if (!foreignOrigin) {
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
					} else {
						console.log(`${ts()} [TaskBridge] ${taskId} is foreign-origin (remote bridge delivers); narrated only, files left for owner bridge`);
					}
				}
			}
		} catch (err) {
			// Directory might not exist yet or file in transit. Log on
			// unusual exceptions (not ENOENT) so a real file-system
			// problem is observable, while still containing the throw.
			const code = (err as NodeJS.ErrnoException)?.code;
			if (code !== 'ENOENT') {
				console.error(`${ts()} [TaskBridge] result-scan threw (non-fatal):`, err);
			}
		}
	}, 2000);
}
