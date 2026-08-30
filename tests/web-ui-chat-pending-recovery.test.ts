import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { CHAT_HTML } from '../src/chat-ui.js';

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const webClient = readFileSync(join(repoRoot, 'src', 'web-client.ts'), 'utf8');

test('dashboard text sends persist and resume pending task results', () => {
	assert.match(webClient, /PERSIST_KEY_CHAT_PENDING = 'sutando-dashboard-chat-pending-v1'/);
	assert.match(webClient, /function addPendingChatSend\(taskId, text\)/);
	assert.match(webClient, /addPendingChatSend\(d\.task_id, text\)/);
	assert.match(webClient, /function resumePendingChatSends\(\)/);
	assert.match(webClient, /pollChatReply\(d\.task_id, placeholder\)/);

	const keyIndex = webClient.indexOf("PERSIST_KEY_CHAT_PENDING = 'sutando-dashboard-chat-pending-v1'");
	const resumeIndex = webClient.lastIndexOf('resumePendingChatSends();');
	assert.ok(keyIndex > 0, 'pending localStorage key should exist');
	assert.ok(resumeIndex > keyIndex, 'resume must run after the pending key is initialized');
});

test('dashboard chat poll tolerates long tasks and does not orphan the reply', () => {
	// Ceiling must be generous — long agent tasks (PR creation, research) exceed
	// a few minutes. A short cap orphaned the reply in the transcript.
	assert.match(webClient, /CHAT_POLL_MAX_MS = 30 \* 60 \* 1000/);
	// Cadence backs off after a fast window instead of hammering /result forever.
	assert.match(webClient, /CHAT_POLL_FAST_WINDOW_MS/);
	assert.match(webClient, /CHAT_POLL_SLOW_MS/);
	// Stale persisted sends are garbage-collected on load.
	assert.match(webClient, /CHAT_PENDING_TTL_MS/);
	// On the hard ceiling the poll must NOT delete the persisted entry — a reload
	// has to be able to re-attach and still render the late reply.
	const pollBody = webClient.slice(
		webClient.indexOf('function pollChatReply'),
		webClient.indexOf('function resumePendingChatSends'),
	);
	assert.doesNotMatch(pollBody, /No response yet/, 'must not show the dead-end placeholder that orphans the reply');
	const ceilingBranch = pollBody.slice(pollBody.indexOf('CHAT_POLL_MAX_MS'));
	assert.doesNotMatch(
		ceilingBranch.slice(0, ceilingBranch.indexOf('fetch(')),
		/removePendingChatSend/,
		'ceiling give-up must keep the persisted entry so a reload can recover',
	);
});

test('/chat sends persist and resume pending task results', () => {
	assert.match(CHAT_HTML, /PENDING_KEY = 'sutando-chat-page-pending-v1'/);
	assert.match(CHAT_HTML, /function rememberPendingTask\(taskId, text\)/);
	assert.match(CHAT_HTML, /rememberPendingTask\(taskId, text\)/);
	assert.match(CHAT_HTML, /function resumePendingTasks\(\)/);
	assert.match(CHAT_HTML, /resumePendingTasks\(\);/);
	assert.match(CHAT_HTML, /pollPendingTask\(taskId, pendingMsg\)/);
});
