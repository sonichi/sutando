#!/bin/bash
# Top-level orchestrator for make-viral-video skill.
# Phases 1-6 of issue #645 (specificity-shape framing per Chi 2026-05-09).
#
# Usage:
#   bash skills/make-viral-video/scripts/build.sh \
#     --topic "DoW UAP file release" \
#     --source "https://war.gov/UFO" \
#     [--target-duration 45] \
#     [--tts-provider GEMINI|OPENAI] \
#     [--out-dir state/viral-{ts}]
#
# Output: state/viral-{ts}/video.mp4 (validated by ffprobe gate before declared)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SKILL_DIR="$REPO/skills/make-viral-video"

TOPIC=""
SOURCE_URL=""
TARGET_DURATION=45
TTS_PROVIDER="GEMINI"
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topic) TOPIC="$2"; shift 2 ;;
    --source) SOURCE_URL="$2"; shift 2 ;;
    --target-duration) TARGET_DURATION="$2"; shift 2 ;;
    --tts-provider) TTS_PROVIDER="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $0 --topic "X" --source "URL" [--target-duration 45] [--tts-provider GEMINI|OPENAI] [--out-dir state/viral-{ts}]
EOF
      exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$TOPIC" ]] || { echo "ERROR: --topic required" >&2; exit 2; }
[[ -n "$SOURCE_URL" ]] || { echo "ERROR: --source required" >&2; exit 2; }

[[ -n "$OUT_DIR" ]] || OUT_DIR="$REPO/state/viral-$(date +%s)"
mkdir -p "$OUT_DIR/artifacts" "$OUT_DIR/fetched_assets" "$OUT_DIR/frames"
LOG="$OUT_DIR/build.log"

# tee everything to build.log
exec > >(tee -a "$LOG") 2>&1
echo "=== make-viral-video build $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "topic: $TOPIC"
echo "source: $SOURCE_URL"
echo "target_duration: ${TARGET_DURATION}s"
echo "tts_provider: $TTS_PROVIDER"
echo "out_dir: $OUT_DIR"

# ----------------------------------------------------------------
# Phase 0.5: Asset-cache preload (best-effort)
# ----------------------------------------------------------------
# If the run dir already has a manifest (e.g. from a prior partial run, or
# core-agent Phase 1 that pre-staged it), preload any cache hits before
# codex/Phase 1 starts so we don't re-fetch identical URLs across runs.
# Per Chi 2026-05-10: "Make existing assets available."
if [[ -f "$OUT_DIR/artifacts/asset_manifest.json" ]]; then
  hits=$(python3 "$SKILL_DIR/scripts/asset_cache.py" preload "$OUT_DIR" 2>&1)
  echo "[Phase 0.5] $hits"
else
  echo "[Phase 0.5] no manifest yet; cache preload deferred to Phase 1.5"
fi

# ----------------------------------------------------------------
# Phase 1: Codex script + asset manifest generation
# ----------------------------------------------------------------
echo ""
echo "--- Phase 1: codex script generation ---"

PROMPT_TEMPLATE="$SKILL_DIR/scripts/codex-prompt.md"
[[ -f "$PROMPT_TEMPLATE" ]] || { echo "ERROR: prompt template missing at $PROMPT_TEMPLATE" >&2; exit 1; }

# Substitute variables into the template, write to tmp file
PROMPT_FILE="$OUT_DIR/codex-prompt.txt"
sed -e "s|{{TOPIC}}|$TOPIC|g" \
    -e "s|{{SOURCE_URL}}|$SOURCE_URL|g" \
    -e "s|{{TARGET_DURATION_S}}|$TARGET_DURATION|g" \
    -e "s|{{ASSET_DIR}}|$OUT_DIR/fetched_assets/|g" \
    -e "s|{{OUTPUT_DIR}}|$OUT_DIR/artifacts/|g" \
    "$PROMPT_TEMPLATE" > "$PROMPT_FILE"

# Run codex /goal exec (codex sandbox can fetch URLs + write files; needs network access)
# Per `feedback_codex_nested_quotes_hang_stdin.md`: pass via "$(cat /tmp/file)" not raw -- arg
codex exec --sandbox workspace-write -o "$OUT_DIR/artifacts/codex-output.txt" -- "$(cat "$PROMPT_FILE")"
echo "[Phase 1] codex done; output at $OUT_DIR/artifacts/codex-output.txt"

# ----------------------------------------------------------------
# Phase 1.5: Cache preload after codex writes manifest
# ----------------------------------------------------------------
# Codex may have failed to fetch some URLs (DNS, 403, etc.). If the cache has
# them from a prior run, copy them in before validation. Idempotent — skipping
# any file that already exists with matching size.
if [[ -f "$OUT_DIR/artifacts/asset_manifest.json" ]]; then
  hits=$(python3 "$SKILL_DIR/scripts/asset_cache.py" preload "$OUT_DIR" 2>&1)
  echo "[Phase 1.5] $hits"
fi

# ----------------------------------------------------------------
# Phase 2: Asset validation
# ----------------------------------------------------------------
echo ""
echo "--- Phase 2: asset validation ---"

VALIDATION_LOG="$OUT_DIR/validation.jsonl"
> "$VALIDATION_LOG"
fail_count=0
total_count=0
for asset in "$OUT_DIR/fetched_assets"/*; do
  [[ -f "$asset" ]] || continue
  total_count=$((total_count + 1))
  result=$(python3 "$SKILL_DIR/scripts/validate_asset.py" "$asset" 2>&1) || true
  echo "$result" >> "$VALIDATION_LOG"
  valid=$(echo "$result" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("valid", False))' 2>/dev/null || echo False)
  if [[ "$valid" != "True" ]]; then
    fail_count=$((fail_count + 1))
    echo "  ✗ $(basename "$asset") — $(echo "$result" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("reason","unknown"))')"
  else
    echo "  ✓ $(basename "$asset")"
  fi
done
echo "[Phase 2] $((total_count - fail_count))/$total_count assets valid"
if [[ $total_count -lt 1 ]]; then
  echo "ERROR: no assets fetched. Codex may have failed silently. See $OUT_DIR/artifacts/codex-output.txt" >&2
  exit 1
fi
if [[ $fail_count -ge 3 ]]; then
  echo "ERROR: $fail_count/$total_count assets failed validation. Aborting before render." >&2
  exit 1
fi

# ----------------------------------------------------------------
# Phase 2.5: Promote validated assets to shared cache
# ----------------------------------------------------------------
# Only files that passed validation reach the cache. URL keys come from the
# manifest. Per Chi 2026-05-10: "Make existing assets available." Future runs
# of any topic that share asset URLs will see cache hits at Phase 0.5/1.5.
promoted=$(python3 "$SKILL_DIR/scripts/asset_cache.py" promote "$OUT_DIR" 2>&1)
echo "[Phase 2.5] $promoted"

# ----------------------------------------------------------------
# Phase 3+5: Frame composition + TTS + ffmpeg encode
# (TTS is invoked from inside render.py)
# ----------------------------------------------------------------
echo ""
echo "--- Phase 5: render (frames + TTS + encode) ---"

python3 "$SKILL_DIR/scripts/render.py" --workdir "$OUT_DIR" --tts-provider "$TTS_PROVIDER"

# ----------------------------------------------------------------
# Phase 6: Pre-publish ffprobe gate
# ----------------------------------------------------------------
echo ""
echo "--- Phase 6: ffprobe gate ---"

# Gate validates structure (h264 + aac + dims + AV sync), not absolute duration.
# Per 2026-05-10 Lucy-Mini iteration: TTS rate varies 117-138 wpm with Aoede;
# the user-supplied --target-duration is a planning hint, not a hard constraint.
# Structural gate stays hard; absolute-duration drift is a soft warning.
if ! python3 "$SKILL_DIR/scripts/verify_video.py" "$OUT_DIR/video.mp4"; then
  echo "ERROR: video failed ffprobe gate. NOT publishing." >&2
  exit 1
fi

# Soft warning if narration drifted ±10s from target
ACTUAL_DUR=$(ffprobe -v quiet -print_format json -show_format "$OUT_DIR/video.mp4" | python3 -c 'import json,sys;print(int(float(json.load(sys.stdin)["format"]["duration"])))')
if [[ -n "$ACTUAL_DUR" ]] && [[ -n "$TARGET_DURATION" ]]; then
  DIFF=$((ACTUAL_DUR - TARGET_DURATION))
  if [[ $DIFF -gt 10 ]] || [[ $DIFF -lt -10 ]]; then
    echo "  [warn] narration drifted ${DIFF}s from target ${TARGET_DURATION}s (actual: ${ACTUAL_DUR}s) — gate passed on AV-sync, but consider tighter prompt"
  fi
fi

# ----------------------------------------------------------------
# Done
# ----------------------------------------------------------------
echo ""
echo "=== BUILD OK ==="
echo "video: $OUT_DIR/video.mp4"
echo "log: $LOG"
echo "validation: $VALIDATION_LOG"

# Emit a verify.sh for independent re-validation
cat > "$OUT_DIR/verify.sh" <<EOFV
#!/bin/bash
# Independent re-validation of this build. Run from the build dir.
set -euo pipefail
cd "\$(dirname "\$0")"
echo "=== Re-validating make-viral-video output ==="
echo "Video:"
python3 "$SKILL_DIR/scripts/verify_video.py" video.mp4 --expected-duration $TARGET_DURATION
echo ""
echo "Assets:"
for a in fetched_assets/*; do
  python3 "$SKILL_DIR/scripts/validate_asset.py" "\$a" >/dev/null && echo "  ✓ \$(basename "\$a")" || echo "  ✗ \$(basename "\$a")"
done
EOFV
chmod +x "$OUT_DIR/verify.sh"
echo "verify: $OUT_DIR/verify.sh"
