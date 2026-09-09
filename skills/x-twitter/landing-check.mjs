// The post-click landing decision, extracted so it can run against a stub page
// with no browser. A click is not a post: X confirms a landing with a toast
// carrying the new /status/ link, and only that link — scoped to the toast —
// counts (a bare /status/ link once matched a stranger's "View analytics").
// Returns a decision; the caller maps it to output + exit code. Keeping the
// `if (!landed)` branch here is load-bearing: without it a timeout reads the
// href off a null handle. tests/x-post-landing-check.test.mjs mutates exactly
// that predicate and asserts this returns wrong.

export const STATUS_HREF = /^(https:\/\/x\.com)?\/[A-Za-z0-9_]+\/status\/\d+$/;

export async function readLanding(page, { timeout = 15000, shotDir, now = Date.now } = {}) {
  let landed = await page
    .waitForSelector('[data-testid="toast"] a[href*="/status/"]', { timeout })
    .catch(() => null);
  if (landed) {
    const h = await landed.getAttribute('href');
    if (!STATUS_HREF.test(h)) landed = null;
  }
  if (!landed) {
    const shot = `${shotDir}/x-post-nolanding-${now()}.png`;
    await page.screenshot({ path: shot });
    const alert = await page.$eval('[role="alert"]', (el) => el.innerText).catch(() => '');
    return {
      posted: false,
      clicked: true,
      reason: 'no /status/ link observed after click',
      alert,
      screenshot: shot,
    };
  }
  const href = await landed.getAttribute('href');
  const url = href.startsWith('http') ? href : `https://x.com${href}`;
  return { posted: true, url };
}

// The decision-to-exit mapping is production logic too: 4 = clicked, nothing
// landed (3 is the pre-click composer refusal). Kept here so a test can pin it;
// left in the caller it was an untestable `process.exit` a mutation could flip to 0.
export const EXIT_NO_LANDING = 4;
export function landingExit(decision) {
  return decision.posted ? 0 : EXIT_NO_LANDING;
}
