---
name: db-browser
description: "Install DB Browser for SQLite (if not already installed) and open a .sqlite file in it. macOS only."
user-invocable: true
---

# DB Browser

Install [DB Browser for SQLite](https://sqlitebrowser.org/) via Homebrew if it isn't already installed, then open a given `.sqlite` file in it. Macos only — uses `brew --cask` + `open -a`.

**Usage**: `/db-browser <path-to.sqlite>`

ARGUMENTS: $ARGUMENTS

## Steps

1. Resolve the path argument. If empty, default to `$SUTANDO_WORKSPACE/data/conversation.sqlite` (falling back to `~/.sutando/workspace/data/conversation.sqlite` when the env var is unset). Bail with an error if the file doesn't exist.
2. Check whether DB Browser for SQLite is installed: `mdfind "kMDItemKind == 'Application'" 2>/dev/null | grep -qi 'DB Browser for SQLite'` (or `[ -d /Applications/DB\ Browser\ for\ SQLite.app ]`). If installed, skip step 3.
3. Install via Homebrew: `brew install --cask db-browser-for-sqlite`. Requires brew. If brew is missing, stop and ask the user to install brew first.
4. If DB Browser is already running on a DIFFERENT db file (its cached snapshot of THAT db won't auto-refresh when you switch files), quit it first: `osascript -e 'tell application "DB Browser for SQLite" to quit'; sleep 2`. Skip if not running.
5. Open the file: `open -a "DB Browser for SQLite" "<resolved-path>"`. Sleep ~2s so the window is up before reporting back.
6. Confirm: print the resolved path + verify DB Browser has the file open via `lsof -c "DB Browser" 2>/dev/null | grep "<resolved-path>"`.

## Notes

- DB Browser caches the schema + first page of data at open time. If the on-disk db file changes underneath (e.g. a migration drops tables, a writer adds rows), the open DB Browser window keeps showing the stale view until you re-open it. That's why step 4 quits first when re-opening — same-file re-open is also a valid refresh trigger.
- WAL flush: if the db is in WAL mode and recent writes haven't merged into the main file, the `.sqlite-wal` sidecar holds them. DB Browser reads both — no checkpoint needed. (Run `sqlite3 <db> 'PRAGMA wal_checkpoint(TRUNCATE);'` only if you want a single-file dump.)
- This skill does NOT screenshot, query, or modify the db. Just install + open. Anything else (screenshot for a PR, run a SQL via Execute SQL, export CSV) is the operator's job in the GUI.
