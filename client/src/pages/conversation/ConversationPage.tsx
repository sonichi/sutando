import { useCallback, useState } from 'react';
import CapabilitiesPanel from '@/components/atoms/capabilities-panel';
import VisionPreview from '@/components/atoms/vision-preview';
import ConversationComposer from '@/components/molecules/conversation-composer';
import ConversationHero from '@/components/molecules/conversation-hero';
import ConversationTopBar from '@/components/molecules/conversation-top-bar';
import KbdHintsRow from '@/components/molecules/kbd-hints-row';
import QuickStartGrid from '@/components/molecules/quick-start-grid';
import ConversationPanels from '@/components/organisms/conversation-panels';
import ConversationStream from '@/components/organisms/conversation-stream';
import ToastOverlay from '@/components/organisms/toast-overlay';
import { APP_COPY } from '@/const-values/app-copy';
import { useAgentSse } from '@/hooks/useAgentSse';
import { useConversation } from '@/hooks/useConversation';
import { useMuteStateSync } from '@/hooks/useMuteStateSync';
import { useStandIdentity } from '@/hooks/useStandIdentity';
import { useTaskPolling } from '@/hooks/useTaskPolling';
import { useTaskToastDriver } from '@/hooks/useTaskToastDriver';
import { useTextSubmit } from '@/hooks/useTextSubmit';
import { useVision } from '@/hooks/useVision';
import { useVoiceAutoReconnect } from '@/hooks/useVoiceAutoReconnect';
import { useVoiceSession } from '@/hooks/useVoiceSession';

/**
 * Conversation page — full redesign.
 *
 * Layout (top to bottom):
 *   .conv-chrome          sticky wrapper — top bar + global panel dock
 *     ConversationTopBar  brand · live indicator · voice
 *     ConversationPanels  pill row launching Tasks / Notes / Asks / Activity
 *                         (drawer slides down below the dock when open)
 *   .conv-main
 *     .conv-hero          idle only — animated orb + greeting + Start voice
 *     KbdHintsRow         hotkey reminders under the hero
 *     QuickStartGrid      idle only — quick-start cards
 *     .conv-stream-card   live only — chat-bubble transcript
 *   .conv-composer-wrap   fixed pill composer at the bottom
 *
 * `.legacy-shell` is retained on the root so existing CSS (avatar
 * `s-{state}` animations, panel internals, toasts) keeps working;
 * `.conv-root` overrides its layout/centering.
 */

type AgentState = 'idle' | 'listening' | 'speaking' | 'working' | 'seeing';

const DASHBOARD_ORIGIN = 'http://localhost:7844';

export default function ConversationPage() {
	const { state: voice, connect, disconnect, toggleMute, getSession } = useVoiceSession();
	const submitText = useTextSubmit(getSession);
	const identity = useStandIdentity();
	useTaskPolling();
	useTaskToastDriver();
	useMuteStateSync({ voiceStatus: voice.status, muted: voice.muted });
	useVoiceAutoReconnect({ voiceStatus: voice.status, connect });

	const avatarPngUrl = identity?.avatarGenerated ? `${DASHBOARD_ORIGIN}/avatar` : undefined;
	const standName = identity?.name ? `Sutando — ${identity.name}` : 'Sutando';
	const tagline =
		identity?.nameOrigin?.split(' — ')[1] ?? identity?.nameOrigin ?? APP_COPY.convTagline;
	const isLive = voice.status === 'live';
	// Vision (screen sharing into the voice session) is only meaningful
	// while voice is live — the hook tears its push session down otherwise.
	const vision = useVision(isLive);
	// Text-only submissions append to the same conversation store the voice
	// stream feeds. Without this, hitting Send in idle mode wiped the
	// composer and surfaced nothing — the user's message + reply went into
	// `entries` but no component rendered them (ConversationStream used to
	// be gated on isLive only). We now show the stream whenever there's
	// transcript content, regardless of voice connection state.
	const { entries } = useConversation();
	const hasTranscript = entries.length > 0;
	const showStream = isLive || hasTranscript;

	const onStartVoice = useCallback(() => connect(), [connect]);
	const onStopVoice = useCallback(() => disconnect(), [disconnect]);
	const onToggleVoice = useCallback(() => {
		if (isLive) disconnect();
		else connect();
	}, [isLive, connect, disconnect]);

	const { agentState: pushedState } = useAgentSse({
		onToggleVoice,
		onToggleMute: toggleMute,
	});
	const agentState: AgentState = pushedState !== 'idle' || isLive ? pushedState : 'idle';

	const [pendingChip, setPendingChip] = useState<string | null>(null);
	const onPickPrompt = useCallback((prompt: string) => setPendingChip(prompt), []);

	return (
		<div className={`legacy-shell conv-root ${isLive ? 'is-live' : ''}`}>
			<div className="sticky top-0 z-30">
				<ConversationTopBar
					standName={standName}
					voiceStatus={voice.status}
					muted={voice.muted}
					watching={vision.watching}
					dashboardUrl={DASHBOARD_ORIGIN}
					onStartVoice={onStartVoice}
					onStopVoice={onStopVoice}
					onToggleMute={toggleMute}
					onToggleWatch={vision.toggleWatch}
				/>
				<ConversationPanels />
			</div>

			<main className="mx-auto flex w-full max-w-[920px] flex-col gap-9 px-6 pb-10 pt-8">
				{showStream ? (
					<ConversationStream errorMessage={voice.errorMessage ?? null} />
				) : (
					<>
						<ConversationHero
							standName={standName}
							tagline={tagline}
							voiceStatus={voice.status}
							agentState={agentState}
							avatarPngUrl={avatarPngUrl}
							onStartVoice={onStartVoice}
						/>
						<CapabilitiesPanel />
						<KbdHintsRow />
						<QuickStartGrid connected={isLive} onPick={onPickPrompt} />
					</>
				)}
			</main>

			<ConversationComposer
				onSubmit={submitText}
				initialValue={pendingChip}
				onConsumeInitial={() => setPendingChip(null)}
				isLive={isLive}
				muted={voice.muted}
				onToggleMute={toggleMute}
			/>

			<VisionPreview stream={vision.stream} frameCount={vision.frameCount} />
			<ToastOverlay />
		</div>
	);
}
