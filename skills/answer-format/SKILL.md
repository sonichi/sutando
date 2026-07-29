# answer-format

Deterministic final-answer normalizer — a last-step pass for any task that ends in a *precise* answer (a number, a short string, a comma-list). Applies the formatting conventions graders and downstream consumers expect without changing meaning.

**Usage**:
```bash
python3 skills/answer-format/scripts/normalize.py --kind number -- "100 million"   # -> 100000000
python3 skills/answer-format/scripts/normalize.py --kind list --sort -- "b, a, c"   # -> a, b, c
echo "  \"Paris\"  " | python3 skills/answer-format/scripts/normalize.py --kind string  # -> Paris
```
Or import it:
```python
from normalize import normalize_answer
normalize_answer("100 million", kind="number")   # "100000000"
```

## What it does

| kind | transforms (all lossless or pattern-gated) |
|---|---|
| `number` | worded magnitude → digits (`3.5 billion`→`3500000000`); strip thousands separators (`1,234`→`1234`); strip a single leading currency (`$`/`€`/`£`/`¥`) or trailing `%` when the rest is numeric |
| `string` | trim whitespace; strip one layer of surrounding quotes; collapse internal whitespace; optionally drop a leading `the`/`a`/`an` (`--drop-article`) — interior words and capitalization untouched |
| `list` | comma-separated, one space after each comma; per-element trim; optional case-insensitive sort (`--sort`) and per-element numeric normalization (`--number-items`) |
| `auto` (default) | infer kind from shape, then apply the above |

## Design principle

**Conservative.** A normalizer that mangles a correct answer is worse than none, so every transform is either lossless or gated on a confident pattern; ambiguous input passes through unchanged. Sorting and article-dropping are opt-in because graders rarely want them.

## Why

Precise answers routinely fail grading or downstream matching on format alone — `"100 million"` vs `"100000000"`, `"1,234"` vs `"1234"`, `'"Paris"'` vs `Paris`. This is a general capability (any task ending in an exact answer benefits); it also implements the final-format step of the agent-eval solver contract, where a single format miss cost a correct L3 answer.
