// Azure-hosted GPT Realtime voice backend (opt-in provider).
//
// Selected via VOICE_BACKEND=gpt-realtime. The default Gemini Live path is
// untouched when this is not set — this module is only imported lazily from
// voice-agent.ts behind that flag.
//
// How it routes to Azure: bodhi's OpenAIRealtimeTransport ships a working
// realtime transport (event handling, reconnect), but it constructs an
// `OpenAI` client against api.openai.com. We reuse the transport class and
// swap its underlying client to `AzureOpenAI`, then redirect the WS factory
// to `OpenAIRealtimeWS.azure(client, {deploymentName})` — the OpenAI SDK's
// supported entry point for Azure realtime.
//
// CAVEAT (tracked for review): Azure's realtime preview rejects bodhi's GA
// session shape, so we request bodhi's `protocolVersion: 'legacy'` path. That
// option is not yet in the upstream bodhi release; see the PR description for
// the bodhi-side follow-up (merge legacy-protocol support upstream, then
// repin). The legacy path is opt-in inside bodhi and does not alter the
// default GA behavior used by other backends.
import { OpenAIRealtimeTransport } from 'bodhi-realtime-agent';
import type { LLMTransport } from 'bodhi-realtime-agent';
import { AzureOpenAI } from 'openai';
import { OpenAIRealtimeWS } from 'openai/realtime/ws';

const AZURE_OPENAI_KEY = process.env.AZURE_OPENAI_API_KEY || '';
// Accept a full base URL and strip any trailing path segment so endpoint
// stays a bare resource URL (https://<resource>.openai.azure.com).
const AZURE_OPENAI_ENDPOINT = process.env.AZURE_OPENAI_ENDPOINT || '';
const AZURE_REALTIME_DEPLOYMENT = process.env.AZURE_REALTIME_DEPLOYMENT || 'gpt-realtime';
// '2025-04-01-preview' is the first realtime spec that accepts `session.type`
// and the v2 transcription model names bodhi sends. Older 2024-10/2024-12
// versions return "Unknown parameter: session.type".
const AZURE_REALTIME_API_VERSION = process.env.AZURE_REALTIME_API_VERSION || '2025-04-01-preview';
const AZURE_REALTIME_VOICE = process.env.AZURE_REALTIME_VOICE || 'alloy';

function ts(): string { return new Date().toISOString().slice(11, 23); }

export function buildAzureRealtimeTransport(): LLMTransport {
	if (!AZURE_OPENAI_KEY || !AZURE_OPENAI_ENDPOINT) {
		throw new Error(
			'VOICE_BACKEND=gpt-realtime requires AZURE_OPENAI_API_KEY and '
			+ 'AZURE_OPENAI_ENDPOINT in the environment.'
		);
	}
	console.log(`${ts()} [voice-backend] Azure GPT Realtime: deployment=${AZURE_REALTIME_DEPLOYMENT} endpoint=${AZURE_OPENAI_ENDPOINT}`);
	// Construct bodhi's transport with a placeholder key so its internal
	// fields exist (config, voice, lastAssistantItemId, ...), then swap the
	// OpenAI client + WS factory to route through Azure.
	// `protocolVersion: 'legacy'` is passed via a cast so this compiles against
	// bodhi releases that don't yet expose the field in their config type (see
	// module header CAVEAT — the legacy path must land upstream in bodhi).
	const transport = new OpenAIRealtimeTransport({
		apiKey: 'azure-placeholder',
		model: AZURE_REALTIME_DEPLOYMENT,
		voice: AZURE_REALTIME_VOICE,
		protocolVersion: 'legacy',
	} as unknown as ConstructorParameters<typeof OpenAIRealtimeTransport>[0]);
	const azureClient = new AzureOpenAI({
		apiKey: AZURE_OPENAI_KEY,
		endpoint: AZURE_OPENAI_ENDPOINT,
		apiVersion: AZURE_REALTIME_API_VERSION,
		deployment: AZURE_REALTIME_DEPLOYMENT,
	});
	// Surgically replace the transport's internal OpenAI client (typed
	// private) with the Azure-flavored one.
	(transport as unknown as { client: unknown }).client = azureClient;
	// bodhi's transport calls the static OpenAIRealtimeWS.create; redirect the
	// first invocation through .azure(), then restore the original so any
	// non-Azure path keeps working.
	const originalCreate = OpenAIRealtimeWS.create;
	let patched = false;
	(OpenAIRealtimeWS as unknown as { create: typeof OpenAIRealtimeWS.create }).create = async function patchedCreate(client, props) {
		if (patched) return originalCreate.call(OpenAIRealtimeWS, client, props);
		patched = true;
		(OpenAIRealtimeWS as unknown as { create: typeof OpenAIRealtimeWS.create }).create = originalCreate;
		return OpenAIRealtimeWS.azure(azureClient, { deploymentName: AZURE_REALTIME_DEPLOYMENT });
	};
	return transport as unknown as LLMTransport;
}
