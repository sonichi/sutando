/**
 * The cloud brain — a fast research side-call for the local voice agent (Tier
 * 0.5). Answers general / current-info questions (news, scores, weather,
 * definitions) with web-search grounding in ~2s, so the agent doesn't have to
 * round-trip through the core.
 *
 * WHY a separate call rather than the live model's own grounding: the local
 * native-audio model (gemini-3.1-live) cannot do Google Search grounding — the
 * (3.1, googleSearch) combo trips a 1011 quota-close on connect, so grounding
 * ships OFF for voice. This side-call uses a text model (gemini-2.5-flash) that
 * grounds cleanly, giving the live agent a research path without touching the
 * voice session's model config.
 *
 * Uses the Generative Language REST API directly (matching browser-tools.ts /
 * recording-tools.ts) rather than the AI SDK — the repo's @ai-sdk/google is a
 * v1-spec provider that its ai@6 (v2-only) rejects for generateText.
 *
 * Kept behind a thin, stable interface so it can grow (memory read-replica,
 * multi-step, streaming) without callers changing.
 */

const CLOUD_BRAIN_MODEL = process.env.CLOUD_BRAIN_MODEL || 'gemini-2.5-flash';

function cloudBrainKey(): string {
  return (
    process.env.GEMINI_API_KEY ||
    process.env.GEMINI_VOICE_API_KEY ||
    process.env.GOOGLE_GENERATIVE_AI_API_KEY ||
    ''
  );
}

export interface ResearchOptions {
  /** 1-3 sentence description of what the user can see, if the question refers
   * to their screen/camera. The cloud brain can't see; this is its window. */
  visualContext?: string;
  /** Override the request timeout (ms). Default 8000. */
  timeoutMs?: number;
}

/**
 * Answer a general / current-info question. Returns a concise, spoken-friendly
 * string (no markdown). Throws on API failure / empty response — callers should
 * catch and degrade gracefully (e.g. delegate to the core agent).
 */
export async function research(query: string, opts: ResearchOptions = {}): Promise<string> {
  const apiKey = cloudBrainKey();
  if (!apiKey) throw new Error('cloud-brain: no GEMINI_API_KEY');

  const visual = opts.visualContext?.trim()
    ? `\n\n[What the user can see right now: ${opts.visualContext.trim()}]`
    : '';
  const prompt =
    'You are a voice assistant answering OUT LOUD in a live call. Answer the question ' +
    'concisely and conversationally in 1-3 sentences — spoken prose, no markdown, no bullet ' +
    'lists, no headings. If it needs current information, use search. If you are unsure, say ' +
    `so briefly rather than guessing.\n\nQuestion: ${query}${visual}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 8000);
  try {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${CLOUD_BRAIN_MODEL}:generateContent?key=${apiKey}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          tools: [{ google_search: {} }], // web-search grounding
        }),
        signal: controller.signal,
      },
    );
    if (!res.ok) {
      throw new Error(`cloud-brain: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
    }
    const data: any = await res.json();
    const parts = data?.candidates?.[0]?.content?.parts;
    const text = Array.isArray(parts)
      ? parts.map((p: any) => p?.text || '').join('').trim()
      : '';
    if (!text) throw new Error('cloud-brain: empty response');
    return text;
  } finally {
    clearTimeout(timer);
  }
}
