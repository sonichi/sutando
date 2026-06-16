/**
 * The Claude Code hook INGEST CONTRACT — the strict wire shape the core's hook
 * (`obs-hook.sh`) POSTs to `/ingest/claude-code-hooks`, plus the decoder that
 * turns an `unknown` body into that typed shape.
 *
 * This is the "detect → strictly type" boundary. The collector hands a
 * normalizer an `unknown` payload; `decodeClaudeCodeHook` validates it's a CC
 * hook (an object with a non-empty string `hook_event_name`) and returns it as
 * the discriminated `ClaudeCodeHook` union, so every downstream `switch` narrows
 * with full type safety — no `as never`, no `Record<string, unknown>` guessing.
 *
 * `ClaudeCodeHook = KnownHook | UnknownHook`:
 *   - KnownHook — one interface per modeled `hook_event_name`, literal-
 *     discriminated, so a `switch` over it narrows each case to exact fields.
 *   - UnknownHook — the forward-compatible escape hatch for not-yet-modeled
 *     events (open index signature). Guard with `isKnownHook` BEFORE the switch
 *     so the known union stays clean.
 *
 * Field names are verified against REAL Claude Code payloads (the docs were
 * wrong on several: PostToolUse output is `tool_response` not `tool_output`;
 * MessageDisplay streams `delta` chunks with a `final` flag; UserPromptSubmit
 * uses `prompt`). Anything unverified rides per-event optionals or the
 * UnknownHook index signature rather than being asserted.
 */

/** Keys present on every hook payload regardless of event. */
export interface HookCommon {
	session_id?: string;
	transcript_path?: string;
	cwd?: string;
	permission_mode?: string;
	ts?: number; // unix seconds, when the payload carries one
}

export interface UserPromptSubmitHook extends HookCommon {
	hook_event_name: 'UserPromptSubmit';
	prompt?: string;
}

export interface UserPromptExpansionHook extends HookCommon {
	hook_event_name: 'UserPromptExpansion';
	// payload shape unverified against real CC output → kept open
	[k: string]: unknown;
}

export interface PreToolUseHook extends HookCommon {
	hook_event_name: 'PreToolUse';
	tool_name: string;
	tool_input?: Record<string, unknown>;
}

export interface PostToolUseHook extends HookCommon {
	hook_event_name: 'PostToolUse' | 'PostToolUseFailure';
	tool_name: string;
	tool_input?: Record<string, unknown>;
	/** REAL field is `tool_response`; `tool_result`/`tool_output` are fallbacks
	 *  from earlier inferred schemas, retained so a payload from any source maps. */
	tool_response?: unknown;
	tool_result?: unknown;
	tool_output?: unknown;
	error?: string;
}

export interface MessageDisplayHook extends HookCommon {
	hook_event_name: 'MessageDisplay';
	message_id?: string;
	turn_id?: string;
	delta?: string; // streamed text chunk
	final?: boolean; // terminal chunk of a message
}

export interface StopHook extends HookCommon {
	hook_event_name: 'Stop';
}

export interface SessionStartHook extends HookCommon {
	hook_event_name: 'SessionStart';
	source?: string;
	model?: string;
}

export interface SessionEndHook extends HookCommon {
	hook_event_name: 'SessionEnd';
	end_reason?: string;
}

export interface PreCompactHook extends HookCommon {
	hook_event_name: 'PreCompact';
	trigger?: string;
}

export interface NotificationHook extends HookCommon {
	hook_event_name: 'Notification';
	notification_type?: string;
}

export interface SubagentStartHook extends HookCommon {
	hook_event_name: 'SubagentStart';
	agent_type?: string;
}

export interface SubagentStopHook extends HookCommon {
	hook_event_name: 'SubagentStop';
	agent_type?: string;
}

export interface TaskCreatedHook extends HookCommon {
	hook_event_name: 'TaskCreated';
	task_id?: string;
	task_title?: string;
}

export interface TaskCompletedHook extends HookCommon {
	hook_event_name: 'TaskCompleted';
	task_id?: string;
}

/** The modeled hooks — a clean discriminated union (no string-typed member), so
 *  a `switch (hook.hook_event_name)` narrows each case to its exact interface. */
export type KnownHook =
	| UserPromptSubmitHook
	| UserPromptExpansionHook
	| PreToolUseHook
	| PostToolUseHook
	| MessageDisplayHook
	| StopHook
	| SessionStartHook
	| SessionEndHook
	| PreCompactHook
	| NotificationHook
	| SubagentStartHook
	| SubagentStopHook
	| TaskCreatedHook
	| TaskCompletedHook;

export type KnownHookEventName = KnownHook['hook_event_name'];

/** Forward-compatible escape hatch: any not-yet-modeled event. Kept OUT of the
 *  `KnownHook` union so its string `hook_event_name` never pollutes narrowing. */
export interface UnknownHook extends HookCommon {
	hook_event_name: string;
	[k: string]: unknown;
}

export type ClaudeCodeHook = KnownHook | UnknownHook;

/** Runtime mirror of `KnownHookEventName` (the type can't produce a Set). The
 *  `Set<KnownHookEventName>` element type makes a typo a compile error. */
export const KNOWN_HOOK_EVENT_NAMES: ReadonlySet<KnownHookEventName> = new Set<KnownHookEventName>([
	'UserPromptSubmit',
	'UserPromptExpansion',
	'PreToolUse',
	'PostToolUse',
	'PostToolUseFailure',
	'MessageDisplay',
	'Stop',
	'SessionStart',
	'SessionEnd',
	'PreCompact',
	'Notification',
	'SubagentStart',
	'SubagentStop',
	'TaskCreated',
	'TaskCompleted',
]);

/**
 * Detect + strictly type. Returns the typed `ClaudeCodeHook` when `payload` is a
 * plausible CC hook (object with a non-empty string `hook_event_name`), else
 * `null` so the collector drops it. The single boundary cast lives HERE, after
 * the runtime check — downstream code is cast-free.
 */
export function decodeClaudeCodeHook(payload: unknown): ClaudeCodeHook | null {
	if (payload === null || typeof payload !== 'object') return null;
	const name = (payload as { hook_event_name?: unknown }).hook_event_name;
	if (typeof name !== 'string' || name.length === 0) return null;
	return payload as ClaudeCodeHook;
}

/** Narrow a decoded hook to the modeled subset. Unknown/forward events fail this
 *  and take the generic `cc.hook.<name>` path. */
export function isKnownHook(hook: ClaudeCodeHook): hook is KnownHook {
	return KNOWN_HOOK_EVENT_NAMES.has(hook.hook_event_name as KnownHookEventName);
}
