// Shared cross-platform temp-file paths used by both writers and readers.
// Must be a single source of truth: voice-agent + open_file write these, and
// recording-tools reads them for subtitle burning / video playback. Hardcoding
// /tmp on one side broke the other on macOS (TMPDIR is /var/folders/...) and
// on Windows (no /tmp at all). Always import from here on both sides.
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const PLAYBACK_PATH = join(tmpdir(), 'sutando-playback-path');
export const VOICE_TRANSCRIPT_PATH = join(tmpdir(), 'sutando-live-transcript-voice.txt');
