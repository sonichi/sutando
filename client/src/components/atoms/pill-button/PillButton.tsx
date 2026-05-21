import type { ButtonHTMLAttributes, ReactNode } from 'react';

/**
 * Generic rounded "pill" button shared by the top bar (Start / End / Mute)
 * and any other chrome control. Variants:
 *   - default: subtle surface tint, neutral text (idle controls)
 *   - primary: inverted monochrome (var(--text) bg, var(--bg) text) — the
 *              same treatment as the hero CTA, used for the only primary
 *              action visible at any time
 *   - danger:  red-tinted (End)
 *   - muted:   red outline (mute is on)
 *   - watching: amber-tinted (screen-share / vision is on)
 *
 * No gradients — primary actions are pure inversion so the brand reads
 * as a single high-contrast surface, not a color story.
 */

type PillVariant = 'default' | 'primary' | 'danger' | 'muted' | 'watching';

export interface PillButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
	variant?: PillVariant;
	children: ReactNode;
}

const BASE =
	'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-3.5 py-1.5 text-xs font-medium transition-[background,border-color,color,filter] duration-150 ease-out disabled:cursor-not-allowed disabled:opacity-60';

const CLASS_BY_VARIANT: Record<PillVariant, string> = {
	default:
		'border border-(--border) bg-(--surface-elev)/80 text-(--text) hover:border-(--text-faint) hover:bg-(--surface-elev)',
	primary:
		'border border-(--text)/10 bg-(--text) text-(--bg) shadow-[0_10px_24px_-14px_rgba(0,0,0,0.6)] hover:brightness-95',
	danger:
		'border border-rose-500/35 bg-rose-500/15 text-rose-100 hover:bg-rose-500/25',
	muted:
		'border border-rose-500/30 bg-rose-500/10 text-rose-300',
	watching:
		'border border-amber-500/40 bg-amber-500/15 text-amber-300 hover:bg-amber-500/25',
};

export default function PillButton({
	variant = 'default',
	className,
	children,
	...rest
}: PillButtonProps) {
	const cls = [BASE, CLASS_BY_VARIANT[variant], className].filter(Boolean).join(' ');
	return (
		<button type="button" {...rest} className={cls}>
			{children}
		</button>
	);
}
