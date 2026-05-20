import PageHeader from '@/components/atoms/page-header';
import SlackTokenForm from '@/components/molecules/slack-token-form';
import { APP_COPY } from '@/const-values/app-copy';
import { APP_ROUTES } from '@/const-values/app-routes';

export default function SettingsPage() {
	return (
		<section className="flex h-full flex-col">
			<PageHeader title={APP_ROUTES.settings.label} hint={APP_ROUTES.settings.hint} />
			<div className="flex-1 space-y-6 overflow-y-auto px-6 py-6">
				<div>
					<h2 className="text-sm font-semibold text-[color:var(--color-text)]">
						{APP_COPY.settingsIntegrationsTitle}
					</h2>
					<div className="mt-3">
						<SlackTokenForm />
					</div>
				</div>
			</div>
		</section>
	);
}
