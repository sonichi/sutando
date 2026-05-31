// #1375 — screen-companion must keep `work` (task delegation) and `switch_mode`
// reachable inside a mode. Both are mainAgentTools (NOT inlineTools), so the
// activation restriction has to filter the FULL session surface, not inlineTools
// alone. Regression guard: before the fix, activate_screen_companion rebuilt the
// surface from inlineTools only and silently dropped work + switch_mode.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { z } from 'zod';
import { setSessionToolUpdater } from '../src/vision-tools.js';
import { workTool } from '../src/task-bridge.js';
import { inlineTools } from '../src/inline-tools.js';
import { tools as scTools } from '../skills/screen-companion/tools.js';

const mk = (name: string) => ({
	name, description: name, parameters: z.object({}), execution: 'inline' as const,
	async execute() { return {}; },
});

test('activate_screen_companion retains work + switch_mode + the mode allow-list (#1375)', async () => {
	// Mirror voice-agent.ts mainAgentTools: workTool + non-inline tools + inlineTools.
	const fullSurface = [workTool, mk('get_task_status'), mk('switch_mode'), mk('save_meeting_note'), ...inlineTools];

	let captured: string[] | null = null;
	setSessionToolUpdater((t) => { captured = (t as any[]).map(x => x.name); }, fullSurface as any);

	const activate = scTools.find(t => t.name === 'activate_screen_companion');
	assert.ok(activate, 'activate_screen_companion tool present');
	try {
		// pair-review-code requires a goal; vision streaming after the restriction
		// may throw without a live session — the restriction has already run by then.
		await activate!.execute({ mode: 'pair-review-code', goal: 'review a PR' } as any);
	} catch { /* post-restriction vision setup */ }

	setSessionToolUpdater(null, []); // cleanup global state for other tests

	assert.ok(captured, 'the tool-surface restriction should have run');
	const surface = captured as unknown as string[];
	assert.ok(surface.includes('work'), 'work (task delegation) must stay reachable in-mode');
	assert.ok(surface.includes('switch_mode'), 'switch_mode must stay reachable in-mode');
	for (const n of ['vision_query', 'take_note', 'look_up_reference']) {
		assert.ok(surface.includes(n), `${n} (mode allow-list) must stay reachable`);
	}
});
