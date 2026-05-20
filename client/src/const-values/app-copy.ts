/**
 * App-wide copy strings. Kept in const-values per CLAUDE.md so components
 * stay strictly presentational. When packages/ui ships (PR-D) the cloud
 * + desktop apps can swap in their own copy without touching components.
 */

export const APP_COPY = {
	appName: 'Sutando',
	appTagline: 'Your personal agent.',
	scaffoldNotice:
		'/ now serves the React build. The original inline HTML is one release away from removal; available at /legacy as an escape hatch.',
	pageMissing: 'No page matched. Use the nav above to pick one.',
	loading: 'Loading…',
	voiceConnect: 'Connect',
	voiceConnecting: 'Connecting…',
	voiceRequestingMic: 'Requesting mic…',
	voiceLive: 'Live',
	voiceError: 'Error',
	voiceIdle: 'Disconnected',
	voiceClosed: 'Disconnected',
	disconnect: 'Disconnect',
	mute: 'Mute',
	unmute: 'Unmute',
	voiceSessionTitle: 'Voice session',
	voiceSessionHint:
		'Connect to start streaming audio to the voice agent on the same machine. Mute pauses outbound audio without dropping the WebSocket.',
	agentStatusTitle: 'Server view',
	agentStatusHint:
		"What voice-agent.ts reports about its own connection state — independent of this browser's mic capture.",
	transcriptTitle: 'Transcript',
	transcriptHint:
		'Live user + assistant transcript. Server-final entries can be copied; in-progress lines fade in as the model speaks.',
	transcriptEmpty: 'No transcript yet — connect and speak to populate.',
	taskListTitle: 'Tasks',
	taskListEmpty: 'No tasks yet — drop something via Discord, Telegram, or the voice agent.',
	taskListAllDoneHidden: 'All tasks complete (hidden). Toggle "show done" to reveal.',
	taskShowDetails: 'Show details ▸',
	taskHideDetails: 'Hide ▾',
	taskShowDone: 'show done',
	taskHideDone: 'hide done',
	taskCollapseAll: 'collapse all',
	taskExpandAll: 'expand all',
	taskSystemBrainOffline: 'brain offline',
	taskSystemWatcherOffline: 'watcher offline',
	taskReplyPlaceholder: 'Type a reply…',
	taskReplyPlaceholderOrType: 'or type a reply…',
	taskReplySend: 'Send',
	taskReplySending: 'Sending…',
	taskReplySent: 'Replied:',
	taskReplyFailed: 'Reply failed —',
	questionsTitle: 'Pending questions',
	questionPlaceholder: 'Or type a response…',
	questionSend: 'Send',
	questionSending: 'Sending…',
	questionAnswered: 'Answered:',
	questionFailed: 'Answer failed —',

	// Modern conversation page
	convGreeting: 'Hey, I’m Sutando.',
	convTagline: 'Your AI partner — voice or text, hands-free or hands-on.',
	convStartVoice: 'Start voice',
	convStopVoice: 'End voice',
	convConnecting: 'Connecting…',
	convRequestingMic: 'Requesting mic…',
	convStateIdle: 'Ready',
	convStateListening: 'Listening',
	convStateSpeaking: 'Speaking',
	convStateWorking: 'Working',
	convStateSeeing: 'Looking',
	convStatusTextOnly: 'Text only',
	convStatusVoiceLive: 'Voice live',
	convStatusConnecting: 'Connecting',
	convStatusError: 'Voice error',
	convQuickStartsLabel: 'Quick starts',
	convPanelsLabel: 'Active work',
	convPanelStarter: 'Starter',
	convPanelTasks: 'Tasks',
	convPanelNotes: 'Notes',
	convPanelQuestions: 'Asks',
	convPanelActivity: 'Activity',
	convPanelDrawerClose: 'Close panel',
	convComposerPlaceholder: 'Ask Sutando anything…',
	convComposerSend: 'Send',
	convDashboardLink: 'Dashboard',
	convOpenSettings: 'Open settings',
	convMute: 'Mute',
	convUnmute: 'Unmute',
	convEnd: 'End',
	convStreamEmpty: 'No messages yet. Try a quick start or speak with the orb.',
	convShortcutDropContext: 'drop context',
	convShortcutDropScreenshot: 'drop screenshot',
	convShortcutVoice: 'voice',
	convShortcutMute: 'mute',

	// Settings — Slack integration
	settingsIntegrationsTitle: 'Integrations',
	slackCardTitle: 'Slack',
	slackCardHint:
		'Connect a Slack app so you can message Sutando from Slack DMs and @mentions. Create an app at api.slack.com/apps with Socket Mode enabled, then paste its two tokens below.',
	slackBotTokenLabel: 'Bot token',
	slackBotTokenPlaceholder: 'xoxb-…',
	slackAppTokenLabel: 'App-level token',
	slackAppTokenPlaceholder: 'xapp-…',
	slackConfigured: 'Configured',
	slackNotConfigured: 'Not configured',
	slackSave: 'Save tokens',
	slackSaving: 'Saving…',
	slackSaved: 'Saved. Restart the Slack bridge to apply.',
	slackSaveFailed: 'Save failed —',
	slackLoadFailed: 'Could not reach the settings endpoint —',
} as const;

export type VoiceStatusKey =
	| 'voiceIdle'
	| 'voiceConnecting'
	| 'voiceRequestingMic'
	| 'voiceLive'
	| 'voiceError'
	| 'voiceClosed';
