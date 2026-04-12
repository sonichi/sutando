/**
 * Shared recording state — used by both browser-tools.ts (describeScreenTool)
 * and recording-tools.ts (scrollAndDescribeTool, screenRecordTool, etc.)
 * to coordinate the demo/recording lifecycle.
 */
export const demoStateRef: { value: 'idle' | 'recording' | 'done' } = { value: 'idle' };
