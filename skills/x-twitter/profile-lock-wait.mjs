// The wait between SIGTERM and SIGKILL, extracted so a test can drive it with a
// fake probe — the same reason readLanding lives outside x-post-browser.mjs.
//
// Chrome writes its cookie jar lazily and flushes on clean shutdown. Killing on a
// fixed 1s timer forces the jar whether or not the flush happened, which drops every
// cookie set since the last write — i.e. exactly the auth cookies from a fresh sign-in.

/** Used when the caller supplies a non-finite or negative grace. */
export const DEFAULT_GRACE_MS = 10000;

/**
 * SIGTERM has been sent; wait for the holders to go, then report what outlived the grace.
 *
 * @param probe    () => {known: boolean, pids: string[]} — an UNKNOWN probe never
 *                 reports "free", so a broken lsof cannot authorise a kill.
 * @param graceMs  how long a holder gets to flush and exit.
 * @param sleep    (ms) => void, injected so a test does not spend real time.
 * @param now      () => epoch ms, injected for the same reason.
 * @returns {{remaining: string[], exitedCleanly: boolean, waitedMs: number}}
 */
export function waitForProfileExit(probe, graceMs, sleep, now = Date.now) {
  const start = now();
  // A non-finite grace makes every `now() >= start + graceMs` false and the loop
  // never exits — `Number('abc')` is NaN, and an env var is the usual source.
  // Garbage must fall back to the DEFAULT, not to 0: a 0 grace kills on the first
  // check, which is the un-flushed SIGKILL this module exists to prevent.
  const grace = Number.isFinite(graceMs) && graceMs >= 0 ? graceMs : DEFAULT_GRACE_MS;
  const deadline = start + grace;
  for (;;) {
    const p = probe();
    const remaining = p.known ? p.pids : [];
    // known && empty is the only state that means "gone"; an unknown probe is not it.
    if (p.known && remaining.length === 0) {
      return { remaining: [], exitedCleanly: true, waitedMs: now() - start };
    }
    if (now() >= deadline) {
      return { remaining, exitedCleanly: false, waitedMs: now() - start };
    }
    sleep(250);
  }
}
