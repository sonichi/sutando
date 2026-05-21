import { Fragment } from 'react';
import { CAPABILITIES, CAPABILITIES_HEADING } from '@/const-values/capabilities';

/**
 * "What I can do" — static capability panel for the idle conversation
 * screen. The parent (ConversationPage) only mounts this in the idle
 * branch, so it is naturally hidden once a voice session or transcript
 * exists — matching the legacy `body.voice-active` hide behavior.
 */
export default function CapabilitiesPanel() {
	return (
		<section className="mx-auto w-full max-w-[480px] rounded-xl border border-(--border) bg-(--surface)/60 px-5 py-3.5">
			<p className="mb-2 text-[10px] uppercase tracking-[0.12em] text-(--text-muted)">
				{CAPABILITIES_HEADING}
			</p>
			<dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
				{CAPABILITIES.map((cap) => (
					<Fragment key={cap.label}>
						<dt className="text-xs whitespace-nowrap text-accent">{cap.label}</dt>
						<dd className="m-0 text-xs text-(--text-muted)">{cap.desc}</dd>
					</Fragment>
				))}
			</dl>
		</section>
	);
}
