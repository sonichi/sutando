/**
 * Shared recording state — used by both browser-tools.ts (describeScreenTool)
 * and recording-tools.ts (scrollAndDescribeTool, screenRecordTool, etc.)
 * to coordinate the demo/recording lifecycle.
 */
export const demoStateRef: { value: 'idle' | 'recording' | 'done' } = { value: 'idle' };

/** True while Gemini is speaking a narration description. Cleared by voice-agent on turn complete. */
export const narrationSpeakingRef: { value: boolean } = { value: false };

/** What Gemini actually said in the last narration turn. Set by voice-agent onTurnCompleted. */
export const lastSpokenRef: { value: string } = { value: '' };

/** Pre-captured next description, ready to inject when Gemini finishes speaking. */
export const nextDescRef: { value: string | null } = { value: null };

/** Scroll pause control — set true to pause, false to resume. */
export const scrollPausedRef: { value: boolean } = { value: false };
