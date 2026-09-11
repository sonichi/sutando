// The defect: releaseProfileLock() SIGTERMed, slept a fixed 1s, then SIGKILLed every
// holder — forcing Chrome down whether or not it had flushed its cookie jar. A
// sign-in's auth cookies are the newest thing in that jar, so they are what is lost.
//
// Run: node tests/x-profile-lock-wait.test.mjs
import { waitForProfileExit } from '../skills/x-twitter/profile-lock-wait.mjs';

let fails = 0;
const ck = (name, cond) => { console.log((cond ? '  ok   ' : '  FAIL ') + name); if (!cond) fails++; };

// A fake clock: sleep advances it, so grace is measured without spending time.
function clock() {
  let t = 0;
  return { now: () => t, sleep: (ms) => { t += ms; } };
}
/** A holder that exits after `afterMs` of grace. */
function holder(afterMs, c, known = true) {
  return () => ({ known, pids: c.now() >= afterMs ? [] : ['4242'] });
}

// 1. A well-behaved Chrome exits on SIGTERM and must NOT be killed — the whole point.
{
  const c = clock();
  const r = waitForProfileExit(holder(500, c), 10000, c.sleep, c.now);
  ck('a holder that exits during grace is NOT force-killed', r.remaining.length === 0 && r.exitedCleanly);
  ck('it returns as soon as the holder is gone, not at the deadline', r.waitedMs < 1000);
}
// 2. The old behaviour must be impossible: at 1s a slow-but-honest Chrome still had it.
{
  const c = clock();
  const r = waitForProfileExit(holder(3000, c), 10000, c.sleep, c.now);
  ck('a holder needing 3s (the old 1s timer killed it) now survives', r.exitedCleanly);
}
// 3. A genuinely stuck holder is still forced — the lock must not regress into a hang.
{
  const c = clock();
  const r = waitForProfileExit(() => ({ known: true, pids: ['99'] }), 2000, c.sleep, c.now);
  ck('a holder that ignores SIGTERM IS reported for SIGKILL', !r.exitedCleanly && r.remaining[0] === '99');
  ck('and only after the full grace period', r.waitedMs >= 2000);
}
// 4. An UNKNOWN probe must never read as "free" — that is what would let a second
//    Chrome launch against a live profile (the corruption this lock exists to prevent).
{
  const c = clock();
  const r = waitForProfileExit(() => ({ known: false, pids: [] }), 1000, c.sleep, c.now);
  ck('an unknown probe never reports a clean exit', r.exitedCleanly === false);
}
// 5. CONTROL — the old implementation must FAIL these. Fixed 1s, then kill regardless.
{
  const c = clock();
  const oldImpl = (probe, _grace, sleep, now) => {
    sleep(1000);
    const p = probe();
    return { remaining: p.known ? p.pids : [], exitedCleanly: false, waitedMs: now() - 0 };
  };
  const r = oldImpl(holder(3000, c), 10000, c.sleep, c.now);
  ck('CONTROL: the old fixed-1s path kills the 3s holder', r.remaining.length === 1 && !r.exitedCleanly);
}

console.log(fails === 0 ? '\nall ok' : `\n${fails} FAILED`);
process.exit(fails === 0 ? 0 : 1);
