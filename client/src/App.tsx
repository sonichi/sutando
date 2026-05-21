import AppShell from '@/components/molecules/app-shell';
import { APP_COPY } from '@/const-values/app-copy';
import type { AppRouteId } from '@/const-values/app-routes';
import { useCurrentRoute } from '@/hooks/useCurrentRoute';
import ConversationPage from '@/pages/conversation';
import CoreCLIPage from '@/pages/core-cli';
import DashboardPage from '@/pages/dashboard';
import SettingsPage from '@/pages/settings';
import SubscriptionsPage from '@/pages/subscriptions';

const renderPage = (id: AppRouteId) => {
	switch (id) {
		case 'conversation':
			return <ConversationPage />;
		case 'core-cli':
			return <CoreCLIPage />;
		case 'dashboard':
			return <DashboardPage />;
		case 'subscriptions':
			return <SubscriptionsPage />;
		case 'settings':
			return <SettingsPage />;
		default:
			return <p className="px-6 py-6 text-sm text-[color:var(--color-text-mute)]">{APP_COPY.pageMissing}</p>;
	}
};

export default function App() {
	const { routeId, setRoute } = useCurrentRoute();
	// The conversation page renders its own legacy chrome (header + hero +
	// bottom panel) — wrapping it in AppShell would double-stack headers
	// and break the fixed bottom input bar. Other routes still get AppShell
	// until they get their own legacy treatment.
	if (routeId === 'conversation') {
		return renderPage(routeId);
	}
	return (
		<AppShell activeId={routeId} onSelect={setRoute}>
			{renderPage(routeId)}
		</AppShell>
	);
}
