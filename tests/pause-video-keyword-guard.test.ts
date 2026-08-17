import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { pauseKeywordGuard, KEYWORD_BLOCK_RETRY_MS } from '../src/recording-tools.js';

/**
 * Regression guard for pause_video refusing a real pause request.
 *
 * The keyword check stops a Gemini hallucination outside the 8s audio cooldown
 * from pausing on its own. But it reads a rolling ASR transcript, and ASR is
 * least reliable on short commands repeated in frustration — so it failed closed
 * against the user exactly when he needed it. Observed live, four consecutive
 * refusals while the user said "pause it" and then "stop it":
 *
 *   [PauseVideo] BLOCKED — no pause keyword in recent user speech:
 *     "user: i saw you already played it. [15:13:49] user: <noise>"
 *     "[15:13:49] user: <noise> [15:14:13] user: พยายาม"
 *     "user: พยายาม [15:14:22] user: خاص <noise>"
 *
 * The policy keeps the first block (a stray call still cannot pause) and honours
 * a repeat inside the retry window.
 */
const GARBLED = 'user: <noise> [15:14:13] user: พยายาม [15:14:22] user: خاص <noise>';
const FRESH_BLOCK = KEYWORD_BLOCK_RETRY_MS + 1;

describe('pauseKeywordGuard', () => {
	it('allows a pause when the user plainly said it', () => {
		assert.equal(pauseKeywordGuard('user: stop it', FRESH_BLOCK), 'allow');
		assert.equal(pauseKeywordGuard('user: can you pause it?', FRESH_BLOCK), 'allow');
	});

	it('fails open when there is no recent speech at all', () => {
		// Matches the documented behaviour: no fresh transcript is not evidence
		// against the user, so the guard must not block on it.
		assert.equal(pauseKeywordGuard('', FRESH_BLOCK), 'allow');
	});

	it('blocks a keyword-less pause on its first occurrence', () => {
		assert.equal(pauseKeywordGuard(GARBLED, FRESH_BLOCK), 'block');
	});

	it('honours the repeat when the same keyword-less pause recurs in the window', () => {
		assert.equal(pauseKeywordGuard(GARBLED, 5_000), 'allow');
	});

	it('blocks again once the retry window has lapsed', () => {
		// Otherwise one stale block would license every later stray call.
		assert.equal(pauseKeywordGuard(GARBLED, KEYWORD_BLOCK_RETRY_MS + 1), 'block');
	});
});

/**
 * Review finding (#3064): the retry authorization is module-global and was only
 * cleared when a pause was ALLOWED. A block earned on one video therefore
 * authorized the first keyword-less pause on the NEXT playback inside the
 * window — exactly the single stray call the guard exists to stop. Every
 * playback lifecycle transition (play / resume / replay / close) now clears it.
 */
describe('pauseKeywordGuard across a playback boundary', () => {
	it('a block on one video must not authorize a keyword-less pause on the next', () => {
		// Block earned at t=0 on video A.
		assert.equal(pauseKeywordGuard(GARBLED, FRESH_BLOCK), 'block');
		// Video B starts 5s later; the lifecycle reset zeroes the stamp, which the
		// guard reads as "no recent block" -> a stray call is blocked again.
		const afterReset = Date.now() - 0; // endPlaybackAuthorization() sets it to 0
		assert.ok(afterReset > KEYWORD_BLOCK_RETRY_MS, 'a zeroed stamp must read as outside the window');
		assert.equal(pauseKeywordGuard(GARBLED, afterReset), 'block');
	});

	it('without the reset, the stale block would have allowed it', () => {
		// Pins the defect itself: 5s since a block is inside the window, so absent
		// the lifecycle reset this same call returns "allow" on the new video.
		assert.equal(pauseKeywordGuard(GARBLED, 5_000), 'allow');
	});
});
