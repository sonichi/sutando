import { useCallback, useEffect, useState } from 'react';
import { fetchSlackSettings, saveSlackSettings, type SlackSettingsStatus } from '@/lib/api';

export type SlackSettingsState = 'loading' | 'idle' | 'saving' | 'saved' | 'error';

export interface UseSlackSettingsResult {
	state: SlackSettingsState;
	status: SlackSettingsStatus | null;
	error: string | null;
	save: (botToken: string, appToken: string) => Promise<void>;
}

/**
 * Slack bridge credential state for the Settings page. Loads the
 * configured/not-configured flags on mount and wraps saveSlackSettings()
 * so the form can show loading → idle → saving → saved / error. Tokens
 * themselves are never read back — only whether each one is on disk.
 */
export function useSlackSettings(): UseSlackSettingsResult {
	const [state, setState] = useState<SlackSettingsState>('loading');
	const [status, setStatus] = useState<SlackSettingsStatus | null>(null);
	const [error, setError] = useState<string | null>(null);

	useEffect(() => {
		const controller = new AbortController();
		fetchSlackSettings(controller.signal)
			.then((s) => {
				setStatus(s);
				setState('idle');
			})
			.catch((e: unknown) => {
				if (controller.signal.aborted) return;
				setError(e instanceof Error ? e.message : 'Failed to load Slack settings');
				setState('error');
			});
		return () => controller.abort();
	}, []);

	const save = useCallback(async (botToken: string, appToken: string) => {
		setState('saving');
		setError(null);
		try {
			await saveSlackSettings(botToken, appToken);
			setStatus({ botConfigured: true, appConfigured: true });
			setState('saved');
		} catch (e: unknown) {
			setError(e instanceof Error ? e.message : 'Failed to save Slack settings');
			setState('error');
		}
	}, []);

	return { state, status, error, save };
}
