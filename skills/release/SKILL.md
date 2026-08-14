---
name: release
description: Prepare, verify, and publish Sutando engine releases. Use when asked to prepare a release proposal, determine a version bump, audit release documentation, validate release gates, create a release PR, tag a confirmed release, or publish GitHub release notes. Preparation is safe and non-publishing; tagging and publishing require explicit owner confirmation.
---

# Release

Use [`docs/release-process.md`](../../docs/release-process.md) as policy. Do not
duplicate or reinterpret it here. This skill turns that policy into an
evidence-backed workflow.

## Choose a mode

- **Prepare**: inspect changes, update documentation and release artifacts, run
  gates, and write a proposal. This is the default.
- **Publish**: create the tag and GitHub Release only after explicit owner
  confirmation in the current conversation.

Never infer publish permission from “prepare,” “get ready,” a green CI run, or
an approved release PR.

## Prepare

1. Read `docs/release-process.md` completely.
2. Resolve the repository and workspace:

   ```bash
   REPO="$(git rev-parse --show-toplevel)"
   WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
   ```

3. Identify the last release tag and the exact candidate SHA. Compare the tag
   to the candidate; do not rely only on PR titles.
4. Classify the change set:
   - user-visible features and fixes;
   - configuration, schema, API, protocol, or permission changes;
   - migrations and rollback implications;
   - new or changed skills;
   - install, setup, operations, and troubleshooting changes.
5. Run the documentation audit:

   ```bash
   python3 skills/release/scripts/docs_audit.py
   ```

6. Perform a docs-impact pass. For every user-visible or contract change,
   identify the canonical document in `docs/catalog.json`. Update that document,
   its `last_verified` date, README navigation when appropriate, migration or
   upgrade guidance, and `CHANGELOG.md`. Do not copy canonical text into a
   second document.
7. Prepare one release PR containing only release artifacts and documentation.
   Do not mix product fixes into it; list unresolved fixes as blockers. It
   records the entry **after** the tag (see Publish step 2) and is not a
   precondition for publishing.
8. Run every applicable hard gate from `docs/release-process.md`, including the
   fresh-clone health check, headline-feature smoke, migration idempotency, and
   prior-release-to-candidate upgrade smoke. Record exact commands, candidate
   SHA, exit status, and observable evidence. Never invent or summarize an
   unexecuted gate as passing.
9. Write the proposal to
   `$WORKSPACE/notes/release-proposals/proposed-vX.Y.Z.md` with:
   - candidate version and SHA;
   - SemVer rationale;
   - user-facing summary and CHANGELOG draft;
   - documentation-impact table;
   - gate evidence;
   - migrations, rollback, and known gaps;
   - unresolved blockers;
   - explicit owner decision line: `publish: pending owner confirmation`.
10. Report the proposal and release PR. Stop before tagging or publishing.

## Publish

Proceed only when the owner explicitly confirms the exact version and candidate
SHA in the current conversation.

1. Re-read the proposal and `docs/release-process.md`.
2. Verify the candidate SHA is still the intended commit, required checks are
   green, the worktree is clean, the tag does not exist, and every blocker is
   resolved.

   **Do not gate the tag on the release PR being merged.** This repo tags the
   candidate commit and records the CHANGELOG entry afterwards — v0.7.0 through
   v0.10.0 were each tagged on a tree containing no entry for themselves, and
   v0.10.0's entry landed two days later in #2826. Holding the tag for the
   changelog PR blocks the release on review latency for no benefit.
3. If any evidence is stale or the SHA changed, return to **Prepare**.
4. Create the annotated tag. Use a signed tag only when the owner is performing
   the signing with their configured key; never impersonate an owner signature.
5. Push the tag and create the GitHub Release from the approved notes.
6. Verify the remote tag and release page, then report their URLs and SHA.

## Safety

- Treat tag creation, tag push, and GitHub Release creation as irreversible
  external writes requiring explicit owner confirmation.
- Never publish from a dirty tree, a moving branch name, or an unrecorded SHA.
- Never bypass CI, approvals, migration gates, or release blockers.
- Keep `CHANGELOG.md` authoritative; generated GitHub notes are a completeness
  aid, not the release narrative.
