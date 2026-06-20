/**
 * Claude Code hooks → spine primitives, as a composable collector Normalizer.
 *
 * Extends `AbstractNormalizer<ClaudeCodeHook>`, so the pipeline reads as
 * "DETECT + strictly type, THEN map":
 *
 *   decode(unknown) → ClaudeCodeHook | null    (cc-hooks: the ingest contract)
 *   map(ClaudeCodeHook) → { events, usage }     (hookMap + MessageDisplay accum)
 *
 * This is the CC-specific plug for the general collector. It owns the
 * CC-isms — the strict hook union, the real field names, MessageDisplay delta
 * accumulation — and emits the universal ObsEvent vocabulary. The collector
 * stays source-agnostic; Claude Code is just one registered source. Per the
 * architecture rules, all Claude-isms live here, not in the collector.
 *
 * Hooks are OBS-ONLY (no authoritative tokens) → `usage` is always empty.
 *
 * MessageDisplay streams in `delta` chunks with a `final` flag, so it's
 * accumulated here (stateful, per message_id) into one `cc.message` per turn —
 * the pure `hookMap` can't. Everything else goes through `hookMap`.
 */

import { AbstractNormalizer, EMPTY, type NormalizeContext, type NormalizeResult } from '../collector/normalizer.js';
import type { ObsEvent } from '../events.js';
import type { ClaudeCodeHook, MessageDisplayHook } from './cc-hooks.js';
import { decodeClaudeCodeHook, isKnownHook } from './cc-hooks.js';
import { traceIdFromSession } from './ids.js';
import { trunc } from './_map-util.js';
import { hookMap } from './hook-map.js';

export const CC_HOOKS_SOURCE = 'claude-code-hooks';

export class ClaudeCodeHookNormalizer extends AbstractNormalizer<ClaudeCodeHook> {
	readonly source = CC_HOOKS_SOURCE;
	private readonly msgBuf = new Map<string, { text: string; sessionId: string; ts: number }>();

	/** Detect + strictly type the raw hook body — the ingest boundary. */
	decode(payload: unknown): ClaudeCodeHook | null {
		return decodeClaudeCodeHook(payload);
	}

	/** Map the typed hook to spine primitives. MessageDisplay is accumulated
	 *  (stateful); everything else is the pure `hookMap`. */
	map(hook: ClaudeCodeHook, ctx: NormalizeContext): NormalizeResult {
		if (isKnownHook(hook) && hook.hook_event_name === 'MessageDisplay') {
			const e = this.accumulate(hook, ctx);
			return e ? { events: [e], usage: [] } : EMPTY;
		}
		return hookMap(hook, ctx);
	}

	/** Buffer streamed MessageDisplay deltas by message id; flush one cc.message
	 *  on the terminal (`final === true`) chunk. */
	private accumulate(p: MessageDisplayHook, ctx: NormalizeContext): ObsEvent | null {
		const id = p.message_id ?? p.turn_id;
		if (!id) return null;
		const buf = this.msgBuf.get(id) ?? { text: '', sessionId: p.session_id ?? 'unknown', ts: ctx.receivedAt };
		if (typeof p.delta === 'string') buf.text += p.delta;
		this.msgBuf.set(id, buf);
		if (this.msgBuf.size > 200) {
			// leak guard: drop the oldest if a `final` never arrives
			const k = this.msgBuf.keys().next().value;
			if (k) this.msgBuf.delete(k);
		}
		if (p.final !== true) return null;
		this.msgBuf.delete(id);
		return {
			schema: 1,
			ts: buf.ts,
			trace_id: traceIdFromSession(buf.sessionId),
			node: ctx.node,
			source: 'core-cli',
			actor: { user_id: 'core', channel: 'claude-code', access_tier: 'owner', tenant_id: null },
			kind: 'cc.message',
			outcome: 'ok',
			data: { text: trunc(buf.text), message_id: String(id) },
		};
	}
}
