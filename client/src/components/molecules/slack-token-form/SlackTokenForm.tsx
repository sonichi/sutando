import { useState } from 'react';
import { APP_COPY } from '@/const-values/app-copy';
import { useSlackSettings } from '@/hooks/useSlackSettings';

const INPUT =
	'mt-1 w-full rounded-md border border-neutral-800/80 bg-[color:var(--color-surface)]/40 ' +
	'px-3 py-2 text-sm text-[color:var(--color-text)] outline-none ' +
	'placeholder:text-[color:var(--color-text-mute)] focus:border-[color:var(--color-accent)] ' +
	'disabled:opacity-50';

const BUTTON =
	'rounded-md border border-[color:var(--color-accent)] bg-[color:var(--color-accent)] ' +
	'px-3 py-1.5 text-sm font-medium text-[color:var(--color-canvas)] ' +
	'disabled:cursor-not-allowed disabled:opacity-40';

/**
 * Settings card for Slack bridge credentials. Collects the bot (`xoxb-`)
 * and app-level (`xapp-`) tokens and POSTs them to `/settings/slack`,
 * which persists `~/.claude/channels/slack/.env`. Token values are never
 * read back — the card only shows a configured/not-configured flag.
 */
export default function SlackTokenForm() {
	const { state, status, error, save } = useSlackSettings();
	const [botToken, setBotToken] = useState('');
	const [appToken, setAppToken] = useState('');

	const isSaving = state === 'saving';
	const canSubmit = botToken.trim().length > 0 && appToken.trim().length > 0 && !isSaving;

	const handleSubmit = (e: React.FormEvent) => {
		e.preventDefault();
		if (!canSubmit) return;
		void save(botToken.trim(), appToken.trim());
	};

	const configuredChip = (configured: boolean) => (
		<span
			className="ml-2 text-xs"
			style={{
				color: configured ? 'var(--color-success)' : 'var(--color-text-mute)',
			}}
		>
			{configured ? APP_COPY.slackConfigured : APP_COPY.slackNotConfigured}
		</span>
	);

	return (
		<form
			onSubmit={handleSubmit}
			className="max-w-prose rounded-lg border border-neutral-800/80 bg-[color:var(--color-surface)]/40 p-5"
		>
			<h3 className="text-sm font-semibold text-[color:var(--color-text)]">
				{APP_COPY.slackCardTitle}
			</h3>
			<p className="mt-1 text-xs text-[color:var(--color-text-dim)]">{APP_COPY.slackCardHint}</p>

			<label className="mt-4 block text-xs font-medium text-[color:var(--color-text)]">
				{APP_COPY.slackBotTokenLabel}
				{status ? configuredChip(status.botConfigured) : null}
			</label>
			<input
				type="password"
				className={INPUT}
				value={botToken}
				onChange={(e) => setBotToken(e.target.value)}
				placeholder={APP_COPY.slackBotTokenPlaceholder}
				disabled={isSaving}
				autoComplete="off"
			/>

			<label className="mt-3 block text-xs font-medium text-[color:var(--color-text)]">
				{APP_COPY.slackAppTokenLabel}
				{status ? configuredChip(status.appConfigured) : null}
			</label>
			<input
				type="password"
				className={INPUT}
				value={appToken}
				onChange={(e) => setAppToken(e.target.value)}
				placeholder={APP_COPY.slackAppTokenPlaceholder}
				disabled={isSaving}
				autoComplete="off"
			/>

			<div className="mt-4 flex items-center gap-3">
				<button type="submit" className={BUTTON} disabled={!canSubmit}>
					{isSaving ? APP_COPY.slackSaving : APP_COPY.slackSave}
				</button>
				{state === 'saved' ? (
					<span className="text-xs" style={{ color: 'var(--color-success)' }}>
						{APP_COPY.slackSaved}
					</span>
				) : null}
				{state === 'error' && error ? (
					<span className="text-xs" style={{ color: 'var(--color-danger)' }}>
						{(status ? APP_COPY.slackSaveFailed : APP_COPY.slackLoadFailed)} {error}
					</span>
				) : null}
			</div>
		</form>
	);
}
