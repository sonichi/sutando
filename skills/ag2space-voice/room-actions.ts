// find/describe/run_room_action: the voice model reaches any action in the AG2 Space catalog beyond
// the room tools in room-read.ts, gated by the effect and risk the server declares for the action.

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import {
	mcpPayload, roomExecute, roomRead, targetRoom, ROOM_COMMONS_TIMEOUT_MS, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_NAME_ERRORS,
	ROOM_READ_CALL_TIMEOUT_MS, WRITE_CONFIRMATION, type ReadRoomDeps, type RoomRef,
} from './room-read.js';

export const ACTION_FIND_LIMIT = 8;
export const ACTION_SUMMARY_MAX = 300;
/** What a generic action hands back to the model, as JSON text. */
export const ACTION_RESULT_MAX_CHARS = 4_000;

/** What voice may do with an action, from the effect and risk the server declares for it. */
export type VoicePolicy = 'read' | 'confirm' | 'no';

export function voicePolicy(effect: unknown, risk: unknown): VoicePolicy {
	if (risk === 'high' || risk === 'critical') return 'no';
	if (effect === 'zero_effect') return 'read';
	if (effect === 'mutation') return 'confirm';
	return 'no';
}

type Scope = 'room' | 'account';
const scopeOf = (s: unknown): Scope => (s === 'account' ? 'account' : 'room');

/** The room an action call targets: none for an account action, else the named or current room. */
async function actionRoom(scope: Scope, room: string | undefined, deps: ReadRoomDeps): Promise<{ ok: true; room: RoomRef | null } | { ok: false; error: string; candidates?: string[]; rooms?: string[] }> {
	if (scope === 'account') return { ok: true, room: null };
	return targetRoom(room, deps);
}

/** A catalog tool (room.actions.search / describe) called directly; errors as codes, never a throw. */
async function catalogCall(deps: ReadRoomDeps, tool: string, scope: Scope, room: RoomRef | null, args: Record<string, unknown>): Promise<{ ok: true; payload: Record<string, unknown> } | { ok: false; error: string }> {
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	try {
		const res = await client.callTool(tool, { ...(scope === 'account' ? { target_kind: 'account' } : { room_id: room!.room_id }), ...args }, ROOM_READ_CALL_TIMEOUT_MS);
		const payload = mcpPayload(res);
		if (res.isError || !payload) return { ok: false, error: String((payload as { code?: string } | null)?.code ?? 'unavailable') };
		return { ok: true, payload };
	} catch (err) {
		const why = err instanceof Error ? err.message : String(err);
		deps.log?.(`[RoomActions] ${tool} failed: ${why}`);
		return { ok: false, error: /timed out/.test(why) ? 'timeout' : 'unavailable' };
	}
}

const cut = (s: unknown, n: number) => { const t = String(s ?? '').replace(/\s+/g, ' ').trim(); return t.length > n ? `${t.slice(0, n)}…` : t; };

export interface FoundAction { action: string; title: string; summary: string; scope: Scope; by_voice: VoicePolicy; needs?: string[] }

export type FindActionsResult =
	| { ok: true; room: string | null; actions: FoundAction[]; ms: number }
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

export async function findActions(args: { query: string; room?: string; scope?: string }, deps: ReadRoomDeps): Promise<FindActionsResult> {
	const t0 = Date.now();
	const query = (args.query ?? '').trim().slice(0, 200);
	if (!query) return { ok: false, error: 'no_query' };
	const scope = scopeOf(args.scope);
	const target = await actionRoom(scope, args.room, deps);
	if (!target.ok) return target;
	const found = await catalogCall(deps, 'room.actions.search', scope, target.room, { query, limit: ACTION_FIND_LIMIT });
	if (!found.ok) return found;
	const items = Array.isArray(found.payload.items) ? found.payload.items : [];
	const actions: FoundAction[] = [];
	for (const raw of items) {
		const a = raw as { action?: unknown; title?: unknown; summary?: unknown; effect?: unknown; risk?: unknown; target_kind?: unknown; required_arguments?: unknown };
		if (typeof a.action !== 'string') continue;
		const needs = Array.isArray(a.required_arguments) ? a.required_arguments.filter((x): x is string => typeof x === 'string') : [];
		actions.push({
			action: a.action, title: cut(a.title, 80), summary: cut(a.summary, ACTION_SUMMARY_MAX), scope: scopeOf(a.target_kind),
			by_voice: voicePolicy(a.effect, a.risk), ...(needs.length ? { needs } : {}),
		});
	}
	return { ok: true, room: target.room?.name ?? null, actions, ms: Date.now() - t0 };
}

export type DescribeActionResult =
	| { ok: true; action: string; title: string; summary: string; by_voice: VoicePolicy; arguments: unknown }
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

/** One action as the server describes it, with the policy derived from its declared effect and risk. */
async function describeRaw(action: string, scope: Scope, room: RoomRef | null, deps: ReadRoomDeps): Promise<{ ok: true; d: Record<string, unknown>; policy: VoicePolicy } | { ok: false; error: string }> {
	const r = await catalogCall(deps, 'room.actions.describe', scope, room, { action });
	if (!r.ok) return r;
	return { ok: true, d: r.payload, policy: r.payload.decision === 'deny' ? 'no' : voicePolicy(r.payload.effect, r.payload.risk) };
}

export async function describeAction(args: { action: string; room?: string; scope?: string }, deps: ReadRoomDeps): Promise<DescribeActionResult> {
	const action = (args.action ?? '').trim();
	if (!action) return { ok: false, error: 'no_action' };
	const scope = scopeOf(args.scope);
	const target = await actionRoom(scope, args.room, deps);
	if (!target.ok) return target;
	const r = await describeRaw(action, scope, target.room, deps);
	if (!r.ok) return r;
	return { ok: true, action, title: cut(r.d.title, 80), summary: cut(r.d.summary, 1_000), by_voice: r.policy, arguments: r.d.input_schema ?? {} };
}

export type RunActionResult =
	| { ok: true; action: string; room: string | null; result: unknown; ms: number }
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

/** A result the model can read: as is when short, else its JSON cut to ACTION_RESULT_MAX_CHARS. */
export function compactResult(result: unknown): unknown {
	const json = JSON.stringify(result ?? null);
	return json.length <= ACTION_RESULT_MAX_CHARS ? result : `${json.slice(0, ACTION_RESULT_MAX_CHARS)}…`;
}

/**
 * Run one catalog action. The policy comes from the server's own description of the action, fetched
 * here — never from the model: a read runs, a change runs (the model has read it back and had a yes),
 * anything high-risk or with an effect outside the room is refused.
 */
export async function runAction(args: { action: string; room?: string; scope?: string; arguments_json?: string }, deps: ReadRoomDeps): Promise<RunActionResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const action = (args.action ?? '').trim();
	if (!action) return { ok: false, error: 'no_action' };
	let callArgs: Record<string, unknown> = {};
	const raw = (args.arguments_json ?? '').trim();
	if (raw) {
		try {
			const v = JSON.parse(raw);
			if (!v || typeof v !== 'object' || Array.isArray(v)) return { ok: false, error: 'bad_arguments' };
			callArgs = v as Record<string, unknown>;
		} catch { return { ok: false, error: 'bad_arguments' }; }
	}
	const scope = scopeOf(args.scope);
	const target = await actionRoom(scope, args.room, deps);
	if (!target.ok) return target;
	const d = await describeRaw(action, scope, target.room, deps);
	if (!d.ok) return d;
	if (d.policy === 'no') return { ok: false, error: 'not_by_voice' };
	const done = d.policy === 'read'
		? await roomRead(client, target.room, action, callArgs, deps, ROOM_COMMONS_TIMEOUT_MS)
		: await roomExecute(client, target.room, action, callArgs, deps);
	if (!done.ok) return done;
	deps.log?.(`[RoomActions] ${action}${target.room ? ` in ${target.room.name}` : ' (account)'} (${d.policy})`);
	return { ok: true, action, room: target.room?.name ?? null, result: compactResult(done.result), ms: Date.now() - t0 };
}

const SCOPE_PARAM = z.enum(['room', 'account']).optional().describe('"account" for an action not tied to one room (as find_room_actions gave it); default "room".');
const ROOM_PARAM = z.string().optional().describe('The room name, e.g. "Sutando Triage". Omit for the current room; ignored for scope "account".');
const ACTION_TIMEOUT = ROOM_INDEX_COLD_TIMEOUT_MS + ROOM_READ_CALL_TIMEOUT_MS + ROOM_COMMONS_TIMEOUT_MS + 1000;

export function createFindActionsTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'find_room_actions',
		description:
			'Find what else you can do in AG2 Space, beyond your other room tools: resolve or list comments, list members, react to a message, page versions, PDFs, folders, databases, invites and more. ' +
			'Call it when the user asks for something in AG2 Space that none of your other tools does. Pass `query` = what the user wants in a few words ("resolve comment", "list members"); `scope` "account" for things not tied to one room (invites, the user directory, search across rooms). ' +
			'Returns {ok:true, actions:[{action, title, summary, scope, by_voice, needs}]}: by_voice "read" — you may run it; "confirm" — it changes something, run it only after a read-back and a yes; "no" — not by voice, say the user can do it in the app. ' +
			'Then call describe_room_action for its arguments, and run_room_action to do it. ' +
			'Errors: ' + ROOM_NAME_ERRORS + '"not_connected", "timeout" or "unavailable" — say you cannot look that up right now.',
		parameters: z.object({ query: z.string().min(1).max(200).describe('What the user wants, in a few words.'), room: ROOM_PARAM, scope: SCOPE_PARAM }),
		execution: 'inline',
		timeout: ACTION_TIMEOUT,
		async execute(args) {
			const a = (args ?? {}) as { query: string; room?: string; scope?: string };
			const r = await findActions(a, deps);
			deps.log?.(`[RoomActions] find "${a.query}" → ${r.ok ? `${r.actions.length} actions in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export function createDescribeActionTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'describe_room_action',
		description:
			'The arguments of one AG2 Space action found by find_room_actions, before running it. Pass `action` and `scope` exactly as found, and the same `room`. ' +
			'Returns {ok:true, action, title, summary, by_voice, arguments} — `arguments` is a JSON schema: its required fields are what run_room_action needs; take ids (event_id, kind) from your earlier tool results, and ask the user for anything else. ' +
			'Errors: "no_action"; "NOT_FOUND" — no such action; ' + ROOM_NAME_ERRORS + '"not_connected", "timeout" or "unavailable".',
		parameters: z.object({ action: z.string().min(1).describe('The action, e.g. "room.commons.comments.resolve".'), room: ROOM_PARAM, scope: SCOPE_PARAM }),
		execution: 'inline',
		timeout: ACTION_TIMEOUT,
		async execute(args) {
			const a = (args ?? {}) as { action: string; room?: string; scope?: string };
			const r = await describeAction(a, deps);
			deps.log?.(`[RoomActions] describe ${a.action} → ${r.ok ? r.by_voice : r.error}`);
			return r;
		},
	};
}

export function createRunActionTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'run_room_action',
		description:
			'Run one AG2 Space action found by find_room_actions, with arguments as describe_room_action showed them. ' +
			'Pass `action` and `scope` exactly as found, the same `room`, and `arguments_json` = a JSON object of the arguments, e.g. {"root_event_id":"$abc"}. ' +
			'An action whose by_voice is "confirm" changes something others see: ' + WRITE_CONFIRMATION.replace('the exact text you will write, on which page and in which room', 'exactly what you will do, to what and in which room') +
			'Returns {ok:true, action, room, result} — tell the user in a sentence what happened or what it found. ' +
			'Errors: "not_by_voice" — the user must do it in the app, say so; "bad_arguments" — arguments_json is not a JSON object; "needs_approval" — the room requires an approval first; "TOOL_INPUT_INVALID" — the arguments do not fit, check describe_room_action; ' + ROOM_NAME_ERRORS +
			'"FORBIDDEN" or "ROOM_ACCESS_DENIED" — say you may not do that there; "not_connected", "timeout" or "unavailable" — say it could not be done right now.',
		parameters: z.object({
			action: z.string().min(1).describe('The action, e.g. "room.commons.comments.resolve".'),
			arguments_json: z.string().optional().describe('The arguments as a JSON object, e.g. {"root_event_id":"$abc"}. Omit when it takes none.'),
			room: ROOM_PARAM,
			scope: SCOPE_PARAM,
		}),
		execution: 'inline',
		timeout: ACTION_TIMEOUT + ROOM_READ_CALL_TIMEOUT_MS,
		async execute(args) {
			const a = (args ?? {}) as { action: string; arguments_json?: string; room?: string; scope?: string };
			const r = await runAction(a, deps);
			deps.log?.(`[RoomActions] run ${a.action} → ${r.ok ? `done in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export const ROOM_ACTIONS_PROMPT_RULE = '- OTHER AG2 SPACE ACTIONS: anything in AG2 Space that none of your room tools does (resolve a comment, list members, react, page versions, invites…) → find_room_actions, describe_room_action for its arguments, then run_room_action; one that changes something follows the same read-back and yes as a write, and "not_by_voice" means the user does it in the app. Never submit work for these.';

export const ROOM_ACTIONS_TURN_CHECK_RULE = '- none: any other AG2 Space action the assistant can run itself (resolve a comment, list members, react, invites) is the assistant\'s own, never work.';
