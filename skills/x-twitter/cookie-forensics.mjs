// Records WHY the X session dies, without ever touching a cookie value.
//
// Measured 2026-09-11: auth_token and ct0 vanished from the profile within ~10
// minutes of a GUI sign-in while the guest rows survived. A selective removal
// like that is what a server-sent clearing Set-Cookie looks like, but three
// theories were falsified before this existed, so it records rather than infers.
//
// NEVER logs a cookie value: names, expiry and the carrying response only.
import { appendFileSync } from 'node:fs';

const AUTH = ['auth_token', 'ct0'];

function write(logPath, row) {
  try {
    appendFileSync(logPath, JSON.stringify({ ts: new Date().toISOString(), ...row }) + '\n');
  } catch { /* forensics must never break a publish */ }
}

/**
 * A Set-Cookie clears a cookie when it empties the value or dates it in the past.
 * The reason is set by whichever rule actually fired, not re-derived from the
 * value afterward — a quoted-empty value and a bare Max-Age=0 both cleared but
 * were misreported as 'past-expiry' when the reason was inferred separately.
 */
export function isClearing(setCookieLine) {
  const [pair, ...attrs] = setCookieLine.split(';').map((s) => s.trim());
  const eq = pair.indexOf('=');
  const name = eq === -1 ? pair : pair.slice(0, eq);
  const value = eq === -1 ? '' : pair.slice(eq + 1);
  if (!AUTH.includes(name)) return null;
  let reason = (value === '' || value === '""') ? 'empty-value' : null;
  for (const a of attrs) {
    const [k, v] = a.split('=').map((s) => s.trim());
    if (/^max-age$/i.test(k) && Number(v) <= 0) reason = reason || 'max-age-0';
    if (/^expires$/i.test(k) && v && new Date(v).getTime() <= Date.now()) reason = reason || 'past-expiry';
  }
  return reason ? { name, reason } : null;
}

/** origin+pathname only — a query string on an auth endpoint routinely carries a token. */
function urlWithoutQuery(url) {
  try {
    const u = new URL(url);
    return u.origin + u.pathname;
  } catch {
    return url.split('?')[0].slice(0, 200);
  }
}

/**
 * Attach to a persistent context. Logs the auth cookies present at launch, every
 * response that clears one, and (via snapshot) what survived at exit.
 */
export function attachCookieForensics(ctx, logPath) {
  let cleared = 0;

  // Both calls below are synchronous and throw immediately on a `ctx` that
  // does not implement the full BrowserContext surface (a real Playwright
  // context always does; a lightweight test double need not). A throw here
  // is not caught by a chained .catch() -- the exception fires before the
  // chain method even returns -- so it would otherwise crash the caller,
  // which is exactly what this module exists to never do.
  try {
    ctx.cookies().then((cs) => {
      write(logPath, {
        event: 'launch',
        auth_present: cs.filter((c) => AUTH.includes(c.name)).map((c) => c.name).sort(),
        total_cookies: cs.length,
      });
    }).catch(() => {});
  } catch { /* forensics must never break a publish */ }

  try {
    ctx.on('response', async (res) => {
      let headers;
      try { headers = await res.headersArray(); } catch { return; }
      for (const h of headers) {
        if (h.name.toLowerCase() !== 'set-cookie') continue;
        // headersArray keeps repeated Set-Cookie separate; a merged value would
        // hide all but the first, which is the one case that matters here.
        for (const line of h.value.split('\n')) {
          const hit = isClearing(line);
          if (!hit) continue;
          cleared++;
          write(logPath, {
            event: 'auth-cookie-cleared',
            cookie: hit.name,
            how: hit.reason,
            by_url: urlWithoutQuery(res.url()),
            status: res.status(),
          });
        }
      }
    });
  } catch { /* forensics must never break a publish */ }

  return async function snapshot(label) {
    try {
      const cs = await ctx.cookies();
      write(logPath, {
        event: 'snapshot',
        label,
        auth_present: cs.filter((c) => AUTH.includes(c.name)).map((c) => c.name).sort(),
        total_cookies: cs.length,
        clearing_responses_seen: cleared,
      });
    } catch { /* ignore */ }
  };
}
