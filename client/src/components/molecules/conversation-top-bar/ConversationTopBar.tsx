import BrandMark from '@/components/atoms/brand-mark';
import LiveIndicator from '@/components/atoms/live-indicator';
import PillButton from '@/components/atoms/pill-button';
import { APP_COPY } from '@/const-values/app-copy';
import type { VoiceSessionStatus } from '@/lib/voice-session';

/**
 * Top chrome of the conversation page. Always renders the brand mark +
 * status; the right-hand controls swap based on whether voice is live:
 *   - idle  → "Start voice" (primary gradient pill)
 *   - live  → Mute toggle + End (danger pill)
 */

export interface ConversationTopBarProps {
	standName: string;
	voiceStatus: VoiceSessionStatus;
	muted: boolean;
	watching: boolean;
	dashboardUrl: string;
	onStartVoice: () => void;
	onStopVoice: () => void;
	onToggleMute: () => void;
	onToggleWatch: () => void;
}

const isVoiceLive = (s: VoiceSessionStatus) => s === 'live';
const isVoiceBusy = (s: VoiceSessionStatus) => s === 'connecting' || s === 'requesting-mic';

export default function ConversationTopBar({
	standName,
	voiceStatus,
	muted,
	watching,
	dashboardUrl,
	onStartVoice,
	onStopVoice,
	onToggleMute,
	onToggleWatch,
}: ConversationTopBarProps) {
	const live = isVoiceLive(voiceStatus);
	const busy = isVoiceBusy(voiceStatus);
	return (
		<header
			className="flex items-center gap-3.5 border-b border-(--border)/70 bg-(--surface)/75 px-5 py-3 backdrop-blur-[14px] backdrop-saturate-140"
		>
			<div className="flex min-w-0 flex-1 items-center gap-2.5">
				<BrandMark />
				<div className="flex min-w-0 flex-col">
					<span className="overflow-hidden text-ellipsis whitespace-nowrap text-sm font-semibold tracking-[-0.01em] text-(--text)">
						{standName}
					</span>
					<span className="flex items-center gap-1.5 text-[11px] text-(--text-muted)">
						<LiveIndicator status={voiceStatus} />
						<a
							href={dashboardUrl}
							target="_blank"
							rel="noreferrer"
							className="border-b border-dotted border-(--text-faint) text-(--text-muted) no-underline hover:text-(--text)"
						>
							{APP_COPY.convDashboardLink}
						</a>
					</span>
				</div>
			</div>

			<div className="flex items-center gap-2">
				{live ? (
					<>
						<PillButton
							variant={muted ? 'muted' : 'default'}
							onClick={onToggleMute}
							aria-pressed={muted}
						>
							{muted ? APP_COPY.convUnmute : APP_COPY.convMute}
						</PillButton>
						<PillButton
							variant={watching ? 'watching' : 'default'}
							onClick={onToggleWatch}
							aria-pressed={watching}
							title="Let Sutando watch your screen"
						>
							{watching ? APP_COPY.convWatching : APP_COPY.convWatch}
						</PillButton>
						<PillButton variant="danger" onClick={onStopVoice}>
							{APP_COPY.convEnd}
						</PillButton>
					</>
				) : (
					<PillButton variant="primary" onClick={onStartVoice} disabled={busy} aria-busy={busy}>
						{busy ? APP_COPY.convConnecting : APP_COPY.convStartVoice}
					</PillButton>
				)}
			</div>
		</header>
	);
}
