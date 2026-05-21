import { memo, useEffect, useRef, useState } from 'react';
import { APP_COPY } from '@/const-values/app-copy';
import { useConversation } from '@/hooks/useConversation';
import { renderChatMarkdown } from '@/lib/chat-markdown';
import type { TranscriptEntry } from '@/types/conversation';

/**
 * Modern chat-bubble transcript. Replaces LegacyTranscript — same auto-
 * stick scrolling behavior, but every entry renders as a typed message
 * bubble (user → right purple, assistant → left neutral, system → centered
 * pill) so the conversation reads like a real chat product instead of a
 * flat "You: ... Sutando: ..." text dump.
 */

const STICK_THRESHOLD_PX = 80;

const ROLE_LABEL: Record<TranscriptEntry['role'], string> = {
	user: 'You',
	assistant: 'Sutando',
	system: 'System',
};

const BUBBLE_BASE =
	'group flex max-w-[78%] flex-col gap-1 rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed break-words';

const BUBBLE_USER =
	'self-end rounded-br-md border border-(--text)/10 bg-(--text) text-(--bg) shadow-[0_12px_24px_-18px_rgba(0,0,0,0.55)]';
const BUBBLE_ASSISTANT =
	'self-start rounded-bl-md border border-(--border) bg-(--surface-elev) text-(--text)';
const BUBBLE_SYSTEM =
	'self-center rounded-full border border-dashed border-(--border) bg-transparent px-2.5 py-1 text-xs text-(--text-muted)';

const classFor = (entry: TranscriptEntry): string => {
	if (entry.role === 'system') return `${BUBBLE_BASE} ${BUBBLE_SYSTEM}`;
	const variant = entry.role === 'user' ? BUBBLE_USER : BUBBLE_ASSISTANT;
	const interim = entry.interim ? 'opacity-60' : '';
	return `${BUBBLE_BASE} ${variant} ${interim}`.trim();
};

function CopyBubble({ text }: { text: string }) {
	const [copied, setCopied] = useState(false);
	const timer = useRef<number | null>(null);
	useEffect(
		() => () => {
			if (timer.current != null) window.clearTimeout(timer.current);
		},
		[]
	);
	const onClick = (ev: React.MouseEvent) => {
		ev.stopPropagation();
		void navigator.clipboard.writeText(text).then(() => {
			setCopied(true);
			if (timer.current != null) window.clearTimeout(timer.current);
			timer.current = window.setTimeout(() => setCopied(false), 1500);
		});
	};
	return (
		<button
			type="button"
			onClick={onClick}
			className="hidden self-end rounded-full border border-current/25 bg-transparent px-2 py-0.5 text-[10px] opacity-70 group-hover:inline-flex"
		>
			{copied ? 'Copied' : 'Copy'}
		</button>
	);
}

/**
 * Finalized user/assistant text renders as sanitized markdown. System
 * pills and still-streaming (interim) entries stay plain text — partial
 * markdown flickers mid-stream, and system copy is never markdown.
 * Memoized on `text` so an unrelated entry update doesn't re-parse every
 * bubble in the transcript.
 */
const MarkdownBody = memo(function MarkdownBody({ text }: { text: string }) {
	return (
		<div
			className="chat-md"
			// renderChatMarkdown runs marked → DOMPurify; output is sanitized.
			dangerouslySetInnerHTML={{ __html: renderChatMarkdown(text) }}
		/>
	);
});

function MessageText({ entry }: { entry: TranscriptEntry }) {
	if (entry.role === 'system' || entry.interim) return <span>{entry.text}</span>;
	return <MarkdownBody text={entry.text} />;
}

function MediaSlot({ entry }: { entry: TranscriptEntry }) {
	if (!entry.media) return null;
	const mime = entry.media.mimeType ?? (entry.media.type === 'video' ? 'video/mp4' : 'image/png');
	const src = `data:${mime};base64,${entry.media.base64}`;
	const caption = entry.media.description;
	if (entry.media.type === 'video') {
		return (
			<div className="mt-1.5">
				<video src={src} controls className="max-w-full rounded-[10px]" />
				{caption ? <div className="mt-1 text-[11px] opacity-70">{caption}</div> : null}
			</div>
		);
	}
	return (
		<div className="mt-1.5">
			<img src={src} alt={caption ?? 'inline image'} className="max-w-full rounded-[10px]" />
			{caption ? <div className="mt-1 text-[11px] opacity-70">{caption}</div> : null}
		</div>
	);
}

export interface ConversationStreamProps {
	errorMessage?: string | null;
}

export default function ConversationStream({ errorMessage }: ConversationStreamProps) {
	const { entries } = useConversation();
	const scrollerRef = useRef<HTMLDivElement | null>(null);
	const stickyRef = useRef(true);

	useEffect(() => {
		const el = scrollerRef.current;
		if (!el) return;
		const onScroll = () => {
			const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
			stickyRef.current = distance < STICK_THRESHOLD_PX;
		};
		el.addEventListener('scroll', onScroll, { passive: true });
		return () => el.removeEventListener('scroll', onScroll);
	}, []);

	useEffect(() => {
		const el = scrollerRef.current;
		if (!el || !stickyRef.current) return;
		el.scrollTop = el.scrollHeight;
	}, [entries.length, entries[entries.length - 1]?.text]);

	const isEmpty = entries.length === 0;

	return (
		<div
			ref={scrollerRef}
			role="log"
			aria-live="polite"
			className="flex max-h-[56vh] min-h-[240px] flex-col gap-2.5 overflow-y-auto rounded-[20px] border border-(--border)/80 bg-(--surface)/85 p-3.5"
		>
			{isEmpty ? (
				<div className="px-3 py-12 text-center text-sm text-(--text-muted)">
					{APP_COPY.convStreamEmpty}
				</div>
			) : (
				entries.map((entry) => {
					const showCopy = !entry.interim && entry.role !== 'system' && entry.text.length > 0;
					return (
						<div key={entry.id} className={classFor(entry)}>
							{entry.role !== 'system' ? (
								<span className="text-[10px] uppercase tracking-[0.06em] opacity-65">
									{ROLE_LABEL[entry.role]}
								</span>
							) : null}
							<MessageText entry={entry} />
							<MediaSlot entry={entry} />
							{showCopy ? <CopyBubble text={entry.text} /> : null}
						</div>
					);
				})
			)}
			{errorMessage ? (
				<div className="self-stretch rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-[13px] text-rose-100">
					{errorMessage}
				</div>
			) : null}
		</div>
	);
}
