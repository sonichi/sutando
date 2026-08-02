// Provider-neutral presenter-mode sentinel policy — TS twin of
// src/presenter_mode.py (#2501). Same contract: the sentinel is active only
// while it contains a FUTURE ISO-8601 UTC expiry; missing, empty, malformed,
// and unreadable sentinels fail closed, and the expiry is exclusive. A
// naturally-expired sentinel is left on disk by design (`presenter-mode.sh
// stop` deletes it, letting the window lapse does not), so any reader that
// tests bare existence re-activates presenter behavior forever after one
// un-stopped talk.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

export function presenterModeActive(workspaceDir?: string, nowIso?: string): boolean {
  const workspace = workspaceDir ?? resolveWorkspace();
  const sentinel = join(workspace, 'state', 'presenter-mode.sentinel');
  let expireIso: string;
  try {
    expireIso = readFileSync(sentinel, 'utf8').trim();
  } catch {
    return false;
  }
  // Full UTC shape, not just a leading digit: '9999-not-a-date' starts with a
  // digit and lexically compares as future, so anything less than the whole
  // pattern lets a corrupted sentinel hold the gate open forever (#2516 review).
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(expireIso)) return false;
  // Shape is not validity. The pattern pins field WIDTHS, so '2026-13-01T…' and
  // '9999-99-99T99:99:99Z' both match and then lexically compare as future.
  // Parsing alone is not enough either: Date ROLLS OVER out-of-range components
  // (2026-02-30 parses fine and means 2026-03-02), which would let a sentinel
  // stand for a different instant than it spells. Require the canonical
  // round-trip, matching the Python twin's strptime + round-trip.
  const parsed = new Date(expireIso);
  if (Number.isNaN(parsed.getTime())) return false;
  if (parsed.toISOString().replace(/\.\d{3}Z$/, 'Z') !== expireIso) return false;
  const now = nowIso ?? new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  return now < expireIso;
}
