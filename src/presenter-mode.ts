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
  const now = nowIso ?? new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
  return now < expireIso;
}
