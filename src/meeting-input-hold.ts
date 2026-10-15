/**
 * In meeting mode bodhi runs the session in transcription mode: the voice model must not
 * speak. bodhi's own injectText already refuses to send then, but text written straight to
 * the Gemini transport bypasses that check and the model answers it aloud. Every direct
 * transport write checks this first.
 */
export function meetingHoldsModel(session: unknown): boolean {
	const s = session as { getTranscriptionMode?: () => string } | null | undefined;
	return typeof s?.getTranscriptionMode === 'function' && s.getTranscriptionMode() === 'transcription';
}
