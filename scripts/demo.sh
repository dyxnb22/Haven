#!/usr/bin/env bash
# Scripted offline demo of Haven. No API key, no network, no side effects
# outside a temporary directory.
#
#   ./scripts/demo.sh              run it
#   asciinema rec demo.cast -c ./scripts/demo.sh    record it
#
# PACE=0 makes it instant (useful in CI); the default pacing is tuned to be
# watchable in a recording.

set -euo pipefail

PACE="${PACE:-1}"
HAVEN="uv run haven"
export HAVEN_DATA_DIR="$(mktemp -d)/haven"
trap 'rm -rf "$(dirname "$HAVEN_DATA_DIR")"' EXIT

step() {
  printf '\n\033[1;36m▸ %s\033[0m\n' "$1"
  sleep "$(echo "$PACE * 0.8" | bc -l 2>/dev/null || echo 0)"
}

run() {
  printf '\033[2m$ %s\033[0m\n' "$*"
  sleep "$(echo "$PACE * 0.4" | bc -l 2>/dev/null || echo 0)"
  "$@" || true
  sleep "$(echo "$PACE * 1.2" | bc -l 2>/dev/null || echo 0)"
}

printf '\033[1mHaven — evidence-driven local coding agent\033[0m\n'
printf 'Everything below is offline and deterministic: no API key is used.\n'

step "1. The claims are executable: 27 offline eval cases, security is a hard gate"
run $HAVEN eval --offline

step "2. Security cases only — every one asserts a specific policy denial"
run $HAVEN eval --offline --category security,injection --out "$HAVEN_DATA_DIR/sec"

step "3. Config provenance: every value says where it came from, secrets never printed"
run $HAVEN config explain

step "4. Context engineering: what the model would see, and why"
run $HAVEN debug-context "fix the failing parser test"

step "5. Environment check, no side effects and no paid calls"
run $HAVEN doctor

printf '\n\033[1;32mDone.\033[0m See docs/EVAL_LIVE.md for the real-model run,\n'
printf 'and docs/DEMO.md for the interactive TUI walkthrough.\n'
