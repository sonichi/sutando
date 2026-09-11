// The defect: releaseProfileLock() SIGTERMed, slept a fixed 1s, then SIGKILLed every
// holder — forcing Chrome down whether or not it had flushed its cookie jar. A
// sign-in's auth cookies are the newest thing in that jar, so they are what is lost.
//
// Run: node tests/x-profile-lock-wait.test.mjs
import { waitForProfileExit, DEFAULT_GRACE_MS } from '../skills/x-twitter/profile-lock-wait.mjs';

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

// 6. A non-finite grace must not hang. `Number('abc')` is NaN and an env var is
//    the usual source; `now() >= start + NaN` is never true, so the loop runs forever.
{
  const c = clock();
  let iters = 0;
  const counting = () => { iters++; if (iters > 1000) throw new Error('did not terminate'); return { known: true, pids: ['7'] }; };
  let threw = null;
  try { waitForProfileExit(counting, Number('abc'), c.sleep, c.now); } catch (e) { threw = e; }
  ck('a NaN grace terminates instead of spinning', threw === null);
  // `iters <= 2` used to stand here. It passed only because NaN collapsed to a 0
  // grace and exited on the first check — it pinned the defect's side effect, not
  // termination. The bound is now grace/sleep (10000/250 = 40) plus slack.
  ck('and it terminates within the grace, not after it', iters > 0 && iters <= 50);
}
// 7. exitedCleanly and remaining DISAGREE under an unknown probe — that disagreement
//    is the flag's whole purpose, so anything recomputing it from `remaining` is wrong.
{
  const c = clock();
  const r = waitForProfileExit(() => ({ known: false, pids: [] }), 1000, c.sleep, c.now);
  ck('unknown probe: remaining is empty', r.remaining.length === 0);
  ck('unknown probe: exitedCleanly is FALSE despite that', r.exitedCleanly === false);
  ck('so remaining.length===0 is NOT a substitute for exitedCleanly',
     (r.remaining.length === 0) !== r.exitedCleanly);
}

// 8. A non-finite grace must fall back to the DEFAULT, not to 0. Falling back to 0
//    makes the first deadline check true, so holders are SIGKILLed with no flush —
//    the un-flushed kill this whole module exists to prevent, restored silently.
{
  const stuck = () => ({ known: true, pids: ['4242'] });
  const sane = (() => { const c = clock(); return waitForProfileExit(stuck, 10000, c.sleep, c.now); })();
  const nan  = (() => { const c = clock(); return waitForProfileExit(stuck, Number('abc'), c.sleep, c.now); })();
  const neg  = (() => { const c = clock(); return waitForProfileExit(stuck, -5, c.sleep, c.now); })();
  ck('a NaN grace waits the DEFAULT, not zero', nan.waitedMs === DEFAULT_GRACE_MS);
  // `10s` is the realistic typo for a *_MS var — `abc` is not what anyone writes.
  // `Infinity` is non-finite the other way and must fall back too, or it hangs.
  for (const raw of ['10s', 'Infinity']) {
    const c2 = clock();
    const r2 = waitForProfileExit(stuck, Number(raw), c2.sleep, c2.now);
    ck(`X_PROFILE_GRACE_MS=${raw} falls back to the DEFAULT`, r2.waitedMs === DEFAULT_GRACE_MS);
  }
  ck('a negative grace waits the DEFAULT too', neg.waitedMs === DEFAULT_GRACE_MS);
  ck('garbage is indistinguishable from a sane default, not from 0',
     nan.waitedMs === sane.waitedMs);
}
// 9. An EXPLICIT 0 is a real choice and must survive. It is the one input that
//    should kill immediately, and conflating it with garbage loses that.
{
  const c = clock();
  const r = waitForProfileExit(() => ({ known: true, pids: ['7'] }), 0, c.sleep, c.now);
  ck('an explicit 0 still means kill immediately', r.waitedMs === 0);
  ck('so 0 and garbage are NOT the same output', r.waitedMs !== DEFAULT_GRACE_MS);
}

console.log(fails === 0 ? '\nall ok' : `\n${fails} FAILED`);
process.exit(fails === 0 ? 0 : 1);
