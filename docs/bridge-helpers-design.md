# Bridge helpers — unified shared primitives

Master design doc tracking the extraction of duplicated bridge primitives (issue [#1335](https://github.com/sonichi/sutando/issues/1335)). Each helper below ships as a focused sub-PR with its own parity test. This doc is the **behavioral contract** that both Python and TypeScript implementations must satisfy — when in doubt, the parity test is the arbiter.

## Why

Each surface (voice / phone / discord / telegram / slack) currently re-implements the same primitives. When a bug is found in one (e.g. #1046 orphan `.sending` recovery, #1235 phone archive missing), it needs the same fix applied in N files. Schema changes (#585 marker proposals, future restart-safety) require N-site edits. The cross-language boundary (TS for voice/phone, Python for discord/telegram/slack) prevents true code sharing, but a **documented contract + parity test** gives equivalent safety.

## What stays out of scope

Per #1335 "NOT NOW" list:

- **Workspace path resolution** — V1 workspace contract (Qingyun's redesign, in flight) rewrites this layer entirely; extracting now is wasted work.
- **Chunking** (Discord 2000 / Slack 4000 / Telegram 4096 char limits) — surface-specific thresholds.
- **Audio streaming** — phone (Twilio) and discord-voice already in their own skills.

## Cross-language contract pattern

For each extracted primitive:

1. **Two parallel implementations** — `src/<helper>.py` (Python) and `src/<helper>.ts` (TypeScript). They share the contract documented here, NOT runtime.
2. **Same function name semantics** — `archive_file()` / `archiveFile()`, `parse_markers()` / `parseMarkers()`, etc. Snake-case vs camelCase per language convention, identical inputs/outputs/observable behavior.
3. **Parity test** — `tests/<helper>-parity.test.py` exercises both impls against shared fixtures, asserts identical filesystem mutations / return values / error states. **Fails CI if either impl drifts.**
4. **Cite the contract** — docstring + comment near each function points to this doc, so future contributors see the cross-language constraint.

## Sub-PRs (ROI-ranked)

### 1. task-archive helper — `src/task_archive.py` + `src/task-archive.ts`

**Status:** Python module file exists from #1299 (only `find_task_file()`); TS module + the actual `archive_file()` extraction are TODO. **Unblocks #1237.**

**Contract:**

```
archive_file(src_path: Path, kind: 'tasks' | 'results', task_id: str) -> None
archiveFile(srcPath: string, kind: 'tasks' | 'results', taskId: string): void
```

**Behavior:**

- If `src_path` does not exist → silent return (no error).
- Otherwise: move `src_path` to `<base>/<kind>/archive/<YYYY-MM>/<task_id>.txt`. Create the parent directory recursively if needed.
- `<base>` is **caller-supplied** (passed as a fourth optional parameter or set at module-import time). Why: TS callsites use `REPO_DIR`, Python callsites use `<workspace>`. This divergence pre-exists the refactor and is left to the V1-workspace redesign to unify; this helper only abstracts the move logic.
- `YYYY-MM` is computed at call time from local time (matches the existing TS impl and Python `datetime.now().strftime("%Y-%m")`).
- On exception: log a warning to stderr (TS: `console.error`, Python: `print(..., flush=True)`) and fall back to `unlink(missing_ok=True)`. The archive is non-critical-path; an unlink-fallback ensures we never leave stale task/result files.
- Idempotent: calling twice with the same args (after the first call moved the file) → second call no-ops because src no longer exists.

**Inputs the helper does NOT take:**

- The archive's parent directory inside `<base>` is hardcoded as `<kind>/archive/<YYYY-MM>/`. Surface-specific archive paths (e.g. discord-bridge's `archive_path()` helper) are wrappers around this primitive that fix `<base>` per-surface.
- The original `find_task_file()` (already in `src/task_archive.py` from #1299) handles the `claim-task.py`'s `.claimed-core-N` rename. Callers chain: `task_file = find_task_file(...) ; archive_file(task_file, ...)`.

**Parity test fixtures:**

- Fixture 1: src exists, kind=tasks → dest = `<base>/tasks/archive/YYYY-MM/<id>.txt`, src removed.
- Fixture 2: src missing → no-op, no exception.
- Fixture 3: dest dir doesn't exist → created recursively before move.
- Fixture 4: move raises (simulate via permission error or read-only filesystem) → src is unlinked, no exception propagated.

**Callsite migration:**

- `src/task-bridge.ts:49` — remove local `archiveFile()`, `import { archiveFile } from './task-archive.js'`.
- `src/discord-bridge.py:~352` — remove local `archive_file()`, `from task_archive import archive_file`.
- `src/telegram-bridge.py:~171` — same.
- `skills/phone-conversation/scripts/conversation-server.ts` (proposed in #1237) — should `import archiveFile from '../../../src/task-archive.js'` rather than inlining `archivePhoneFile`. **Track as a follow-up rebase** of #1237.

### 2. proactive-delivery sweep — `src/proactive_delivery.py`

**Why Python-only:** Proactive-`*.txt` files are delivered only by the Python bridges (discord/telegram/slack). TS-side voice agents don't deliver proactive messages.

**Status:** TODO. Currently duplicated in `src/discord-bridge.py:~3322-3331` + `_recover_orphan_sending_files()` (#1046) and `src/telegram-bridge.py:~531-537` + same helper.

**Contract:**

```
claim_proactive_file(src: Path, surface: str, instance: str) -> Path | None
sweep_orphan_sending_files(proactive_dir: Path, surface: str, instance: str) -> int
```

- `claim_proactive_file`: atomically rename `proactive-<ts>.txt` → `proactive-<ts>.sending-<surface>-<instance>`. Returns the new path on success, `None` if already claimed by another instance.
- `sweep_orphan_sending_files`: at startup, find any `.sending-<surface>-<instance>` files belonging to this instance (from a prior crashed run) and re-claim them for delivery. Returns count recovered.

**Behavioral details to extract verbatim from #1046's existing impls:**

- Sentinel TTL: how long a `.sending-*` file is considered "in flight" vs "orphan" (current discord impl: anything older than 5min when this process didn't write it → orphan).
- Instance identifier format (hostname or SUTANDO_HOST_LABEL).
- Failure mode on rename collision.

### 3. result markers — `src/result_markers.py` (and TS counterpart, TODO)

**Status:** **Python side already shipped in #873** — `src/result_markers.py` exists with `parse_markers()`. `discord-bridge.py`, `slack-bridge.py`, and `telegram-bridge.py` already `from result_markers import parse_markers`. TypeScript-side equivalent (`src/result-markers.ts`) for `task-bridge.ts` and the two voice surfaces (`phone-conversation`, `discord-voice`) is TODO if/when those surfaces need the same parse-time discipline.

**Contract:**

```python
@dataclass
class MarkerSet:
    deduped: str | None         # task-id to consolidate replies under, if [deduped:] present
    no_send: bool                # True if body starts with [no-send]
    replied: bool                # True if body starts with [REPLIED]
    channel_redirect: str | None # target channel ID if first line is [channel: ID]
    files: list[str]             # paths from [file: ...], [send: ...], [attach: ...]

def parse_markers(body: str) -> tuple[MarkerSet, str]
```

```typescript
type MarkerSet = {
  deduped: string | null;
  noSend: boolean;
  replied: boolean;
  channelRedirect: string | null;
  files: string[];
};

function parseMarkers(body: string): [MarkerSet, string]
```

**Behavior:**

- Parse leading-line markers in order: `[deduped:]`, `[no-send]`, `[REPLIED]`, `[channel: ID]`.
- Body lines starting with `[file: /path]` / `[send: /path]` / `[attach: /path]` collected into `files`.
- Returns `(MarkerSet, stripped_body)` — caller decides delivery action from `MarkerSet`, sends `stripped_body` (with marker lines removed) as the user-facing content.
- Markers are mutually exclusive in semantics (a `[deduped:]` task should not also produce a user-visible reply); the parser does NOT enforce this — surfaces decide.

**Parity test fixtures** (per CLAUDE.md "Result-body protocol markers" spec):

- Empty body → empty MarkerSet, unchanged body.
- `[deduped: task-X]\n` → `deduped="task-X"`, stripped body = "".
- `[no-send]\nbody` → `noSend=true`, stripped body = "body".
- `[channel: 1234567890]\nrest` → `channelRedirect="1234567890"`, stripped body = "rest".
- `[file: /tmp/x.png]\nbody` → `files=["/tmp/x.png"]`, stripped body retains the `[file:]` line (existing surfaces re-extract).
- Markers in arbitrary positions: only the FIRST non-empty line is checked for `[channel:]` per CLAUDE.md spec; deduped/no-send/REPLIED only match at body start.

### 4. task-file write format — `src/task_format.py` + `src/task-format.ts`

**Status:** TODO. Each bridge + voice-agent hand-rolls the body. Typo or missed field = silent consumer break.

**Contract:**

```python
def write_task(
    out_path: Path, *,
    id: str, timestamp: str, task: str,
    source: str, channel_id: str, channel_name: str | None = None,
    guild_name: str | None = None, source_message_id: str | None = None,
    user_id: str, access_tier: Literal['owner', 'team', 'other'],
    priority: Literal['urgent', 'normal', 'low'] = 'normal',
    extras: dict[str, str] | None = None,
) -> None
```

```typescript
function writeTask(outPath: string, fields: TaskFields): void
```

**Behavior:**

- Atomic write (tmpfile + rename) — task-bridge / discord-bridge consumers must never observe a partially-written task file.
- Field order is fixed (matches existing `id: / timestamp: / task: / source: / channel_id: ...`).
- Optional fields are omitted from output when None/null.
- `extras` allows surface-specific fields (e.g. `source_message_id` from Discord, `tg_chat_id` from Telegram) without bloating the core schema.

### 5. access-tier resolver — `src/access_tier.py`

**Why Python-only:** Access control runs at bridge ingress (Python). TS-side voice/phone surfaces don't have tiers — they're always owner.

**Status:** TODO. Currently `src/discord-bridge.py` and `src/telegram-bridge.py` each read `~/.claude/channels/<surface>/access.json` and inject their own tier-specific system instructions.

**Contract:**

```python
def resolve_tier(surface: str, user_id: str) -> Literal['owner', 'team', 'other']

def system_instructions_for_tier(tier: Literal['owner', 'team', 'other']) -> str | None
```

**Behavior:**

- `resolve_tier`: look up `user_id` in `~/.claude/channels/<surface>/access.json` (`allowFrom` + `tierMap`). Owner tier returned if user_id is in `allowFrom` and absent from `tierMap` (preserves pre-tierMap behavior). Returns `'other'` if user_id is not in `allowFrom`.
- `system_instructions_for_tier`: returns the in-band `===SUTANDO SYSTEM INSTRUCTIONS===` block to embed in non-owner task files. None for owner (no extra instructions needed).

**Parity test:** N/A (Python-only). Unit test verifies tier resolution for each surface's access.json schema + the injected instruction content for team vs other.

## Sub-PR landing order

1. **task-archive** (sub-PR-1) — smallest, unblocks #1237.
2. **proactive-delivery** (sub-PR-2) — Python-only, consolidates #1046's two-site fix.
3. **result-markers** (sub-PR-3) — highest schema-change risk.
4. **task-format** (sub-PR-4) — depends on (3) for marker parsing on the write side.
5. **access-tier** (sub-PR-5) — Python-only, no cross-language constraint.

Each sub-PR is independently revertible. None depends on a later sub-PR.

## Non-goals

- **Performance optimization.** The original impls are I/O bound on disk move/parse; the unified impls preserve identical I/O patterns.
- **API additions.** This refactor exposes exactly the methods listed above. New functionality (e.g. archive compression, parallel sweep) is out of scope.
- **Test-coverage growth.** Parity tests assert behavioral equivalence between Python and TS. Additional unit tests for new edge cases land separately.

## Open questions (none blocking sub-PR-1)

- **Module path from skills:** `skills/phone-conversation/scripts/conversation-server.ts` imports from `src/task-archive.ts` will need `'../../../src/task-archive.js'`. Verify this resolves cleanly under tsx + the current `tsconfig.json` `paths` config. If not: re-export from `skills/_shared/index.ts` (out-of-scope for sub-PR-1, file as follow-up).
- **#1237 rebase strategy:** Once sub-PR-1 lands, do we rebase #1237 onto it (cleaner final state, blocks #1235 fix until then) or merge #1237 first with the local `archivePhoneFile` (faster #1235 fix, sub-PR-1 deletes the 4th copy after)? **Recommend (B) merge as-is** — #1235 has been bleeding for weeks; the temporary 4th copy is fine for a few days.

---

Issue: [#1335](https://github.com/sonichi/sutando/issues/1335)
Owner-driven scope: Susan 2026-05-29 — "refactor 但是不能 break". Each sub-PR ships with a parity test; main branch never sees a half-extracted state.
