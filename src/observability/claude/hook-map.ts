/**
 * `hookMap(hook, ctx)` — map ONE strictly-typed Claude Code hook to spine
 * primitives. Input is the decoded `ClaudeCodeHook` union (see `cc-hooks.ts`),
 * so every case narrows to its exact interface — no `as`, no field guessing.
 *
 * Hooks are OBS-ONLY (lifecycle / tool decisions; no authoritative tokens), so
 * `usage` is always empty. Unknown / not-yet-modeled events take a forward-
 * compatible `cc.hook.<snake(name)>` branch carrying every non-envelope field.
 * File-op tool hooks (Read/Write/Edit) additionally emit a paired `file.*`
 * event — this is how core-cli file consumption is recorded without
 * instrumenting the core. Pure (no Date.now()/random; ts comes via ctx).
 */

import type { ObsEvent, Outcome } from '../events.js';
import type { MapContext, MapResult } from './cc-records.js';
import type { ClaudeCodeHook } from './cc-hooks.js';
import { isKnownHook } from './cc-hooks.js';
import { baseEvent, clean, fileOpFor, nonCommon, pathFromToolInput, trunc, tsOf } from './_map-util.js';

function snake(s: string): string {
	return s.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
}

export function hookMap(hook: ClaudeCodeHook, ctx: MapContext): MapResult {
	const ts = tsOf(typeof hook.ts === 'number' ? hook.ts : undefined, ctx);
	const sessionId = hook.session_id;
	const common = { session_id: sessionId, cwd: hook.cwd, permission_mode: hook.permission_mode };
	const events: ObsEvent[] = [];

	const ev = (kind: string, outcome: Outcome, data: Record<string, unknown>): ObsEvent => {
		const e = baseEvent({ sessionId, kind, outcome, ctx, ts });
		e.data = clean({ ...common, ...data });
		return e;
	};

	// Unknown / not-yet-modeled event → forward-compatible generic capture.
	// Guard FIRST so the `switch` below sees only the clean `KnownHook` union.
	if (!isKnownHook(hook)) {
		events.push(ev('cc.hook.' + snake(hook.hook_event_name), 'ok', nonCommon(hook)));
		return { events, usage: [] };
	}

	switch (hook.hook_event_name) {
		case 'PreToolUse':
			events.push(ev('tool.call', 'ok', { tool_name: hook.tool_name, tool_input: trunc(hook.tool_input) }));
			break;
		case 'PostToolUse':
		case 'PostToolUseFailure': {
			const outcome: Outcome = hook.hook_event_name === 'PostToolUseFailure' ? 'error' : 'ok';
			events.push(
				ev('tool.result', outcome, {
					tool_name: hook.tool_name,
					tool_input: trunc(hook.tool_input),
					// real field is `tool_response`; fall back to the inferred names
					tool_output: trunc(hook.tool_response ?? hook.tool_result ?? hook.tool_output),
					error: hook.error,
				}),
			);
			const fo = fileOpFor(hook.tool_name);
			const path = pathFromToolInput(hook.tool_input);
			if (fo && path) {
				const fe = baseEvent({ sessionId, kind: fo.kind, outcome, ctx, ts });
				fe.source_file = path;
				fe.data = clean({ ...common, op: fo.op, tool_name: hook.tool_name });
				events.push(fe);
			}
			break;
		}
		case 'UserPromptSubmit':
			events.push(ev('cc.prompt', 'ok', { prompt: trunc(hook.prompt) }));
			break;
		case 'UserPromptExpansion':
			// field shape unverified — capture whatever it actually sends
			events.push(ev('cc.prompt_expansion', 'ok', nonCommon(hook)));
			break;
		case 'MessageDisplay':
			// streamed in `delta` chunks; accumulated into one `cc.message` by the
			// normalizer (stateful) — never produces an event from the pure mapper.
			break;
		case 'Stop':
			events.push(ev('cc.hook.stop', 'ok', {}));
			break;
		case 'SessionStart':
			events.push(ev('cc.hook.session_start', 'ok', { source: hook.source, model: hook.model }));
			break;
		case 'SessionEnd':
			events.push(ev('cc.hook.session_end', 'ok', { end_reason: hook.end_reason }));
			break;
		case 'PreCompact':
			events.push(ev('cc.hook.pre_compact', 'ok', { trigger: hook.trigger }));
			break;
		case 'Notification':
			events.push(ev('cc.hook.notification', 'ok', { notification_type: hook.notification_type }));
			break;
		case 'SubagentStart':
			events.push(ev('cc.hook.subagent_start', 'ok', { agent_type: hook.agent_type }));
			break;
		case 'SubagentStop':
			events.push(ev('cc.hook.subagent_stop', 'ok', { agent_type: hook.agent_type }));
			break;
		case 'TaskCreated':
			events.push(ev('cc.hook.task_created', 'ok', { task_id: hook.task_id, task_title: hook.task_title }));
			break;
		case 'TaskCompleted':
			events.push(ev('cc.hook.task_completed', 'ok', { task_id: hook.task_id }));
			break;
	}

	return { events, usage: [] };
}
