import { useCallback, useMemo, useState } from 'react';
import AvatarSvgDefault from '@/components/atoms/avatar-svg-default';
import QuickStartCard from '@/components/atoms/quick-start-card';
import VisionPreview from '@/components/atoms/vision-preview';
import ConversationComposer from '@/components/molecules/conversation-composer';
import ConversationTopBar from '@/components/molecules/conversation-top-bar';
import ConversationPanels from '@/components/organisms/conversation-panels';
import ConversationStream from '@/components/organisms/conversation-stream';
import ToastOverlay from '@/components/organisms/toast-overlay';
import { APP_COPY } from '@/const-values/app-copy';
import { pickQuickStarts } from '@/const-values/quick-starts';
import { useAgentSse } from '@/hooks/useAgentSse';
import { useMuteStateSync } from '@/hooks/useMuteStateSync';
import { useStandIdentity } from '@/hooks/useStandIdentity';
import { useTaskPolling } from '@/hooks/useTaskPolling';
import { useTaskToastDriver } from '@/hooks/useTaskToastDriver';
import { useTextSubmit } from '@/hooks/useTextSubmit';
import { useVision } from '@/hooks/useVision';
import { useVoiceAutoReconnect } from '@/hooks/useVoiceAutoReconnect';
import { useVoiceSession } from '@/hooks/useVoiceSession';

/**
 * Conversation page — ChatGPT-style chat shell.
 *
 * Layout (top to bottom, flex column filling the viewport):
 *   sticky chrome
 *     ConversationTopBar  brand · live indicator · voice controls
 *     ConversationPanels  pill row launching Tasks / Notes / Asks / Activity
 *   main (flex-1)
 *     ConversationStream  always rendered — fills available height; when
 *                         empty shows the compact greeting + suggestion
 *                         cards passed via `emptyState`
 *   ConversationComposer  fixed pill at the bottom
 *
 * The previous design had a separate "idle splash" mode (big animated
 * orb + Start voice CTA + capabilities + quick-start grid) that swapped
 * in when there was no transcript. That made the page feel like a voice
 * dashboard instead of a chat. The thread is now permanent; idle state
 * lives inside it as a centered empty state.
 *
 * `.legacy-shell` is retained on the root so the panel organisms' CSS
 * and the legacy avatar `s-{state}` ring animations keep working.
 */

const DASHBOARD_ORIGIN = 'http://localhost:7844';

const EMPTY_STATE_CARD_COUNT = 4;

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
	const heading = standName.startsWith('Sutando — ')
		? standName.replace('Sutando — ', '')
		: APP_COPY.convGreeting;
	const isLive = voice.status === 'live';
	const vision = useVision(isLive);

	const onStartVoice = useCallback(() => connect(), [connect]);
	const onStopVoice = useCallback(() => disconnect(), [disconnect]);
	const onToggleVoice = useCallback(() => {
		if (isLive) disconnect();
		else connect();
	}, [isLive, connect, disconnect]);

	// SSE bridge for global hotkeys (Cmd-V → toggle voice, etc.). The
	// pushed `agentState` is no longer rendered on this page now that the
	// big animated orb is gone; the hook is invoked purely for its side
	// effect of wiring keyboard shortcuts.
	useAgentSse({ onToggleVoice, onToggleMute: toggleMute });

	const [pendingChip, setPendingChip] = useState<string | null>(null);
	const onPickPrompt = useCallback((prompt: string) => setPendingChip(prompt), []);

	const quickStarts = useMemo(
		() => pickQuickStarts(new Date().getHours(), isLive).slice(0, EMPTY_STATE_CARD_COUNT),
		[isLive],
	);

	const emptyState = (
		<div className="m-auto flex w-full max-w-[640px] flex-col items-center gap-6 px-4 py-10 text-center">
			<div className="flex h-16 w-16 items-center justify-center overflow-hidden rounded-full border border-(--border) bg-(--surface-elev) shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_18px_40px_-26px_rgba(0,0,0,0.55)]">
				{avatarPngUrl ? (
					<img src={avatarPngUrl} alt={standName} className="h-full w-full object-cover" />
				) : (
					<div className="h-[78%] w-[78%]">
						<AvatarSvgDefault />
					</div>
				)}
			</div>
			<div className="flex flex-col gap-2">
				<h1 className="m-0 text-[26px] font-semibold tracking-[-0.02em] text-(--text)">
					{heading}
				</h1>
				<p className="m-0 max-w-[440px] text-[14px] leading-relaxed text-(--text-muted)">
					{tagline || APP_COPY.convTagline}
				</p>
			</div>
			{quickStarts.length > 0 ? (
				<div className="grid w-full grid-cols-1 gap-2.5 sm:grid-cols-2">
					{quickStarts.map((q) => (
						<QuickStartCard key={q.id} quickStart={q} onPick={onPickPrompt} />
					))}
				</div>
			) : null}
		</div>
	);

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

			<main className="mx-auto flex w-full max-w-[920px] flex-1 flex-col px-6 pt-2">
				<ConversationStream
					errorMessage={voice.errorMessage ?? null}
					emptyState={emptyState}
				/>
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
