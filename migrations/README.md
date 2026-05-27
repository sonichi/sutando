# Sutando Migration Scripts

Migrations run once at startup (via `src/startup.sh`) and are tracked in
`$SUTANDO_WORKSPACE/state/schema-version.json`. Applied migrations never run again.

## Naming convention

```
NNNN-short-description.{sh,py}
```

- `NNNN` is a zero-padded 4-digit number, starting at `0001`.
- Each number must be exactly 1 greater than the current highest number in `main`.
- Two open PRs proposing the same number → CI fails (`tests/migrations/test_no_collision.py`).

## Per-script contract

| Requirement | Detail |
|---|---|
| **Idempotent** | Running the script a second time must be safe — check before mutating. |
| **Exit 0** | Signals success; runner records the migration as applied. |
| **Exit non-zero** | Signals failure; runner aborts startup and prints script path + exit code + stderr tail. |
| **No side-effects on dry-run** | Scripts SHOULD respect `DRY_RUN=1` env and print what they would do without doing it. |

## State file

`$SUTANDO_WORKSPACE/state/schema-version.json`:

```json
{
  "applied": [1, 2, 3],
  "current": 3,
  "engine_version_at_apply": "v0.1.0"
}
```

Missing file → treated as `{"applied": [], "current": 0}` (fresh install).

## Running manually

```bash
python3 src/run_migrations.py            # apply pending, exit 0 if none
python3 src/run_migrations.py --dry-run  # print pending without applying
```
