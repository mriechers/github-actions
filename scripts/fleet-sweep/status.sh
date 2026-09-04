#!/usr/bin/env bash
# status.sh — what SHA is each in-scope repo actually pinned to? (read-only)
#
# The confirmation half of a release: after a sweep, this says which repos
# carry the new pin, which are stale, and which never had the workflow at all.
# Replaces the-lodge's audit-claude-automations.sh, which enumerated three
# hard-coded owners rather than the derived scope, so two owners were invisible
# to it.
#
# Usage:  scripts/fleet-sweep/status.sh              # compare against the stubs' pin
#         EXPECT=<sha> scripts/fleet-sweep/status.sh # compare against a specific release
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
EXPECT="${EXPECT:-$(grep -oE '[0-9a-f]{40}' "$DIR/stubs/claude-code-review.yml" | head -1)}"
[ -n "$EXPECT" ] || { echo "ERROR: could not determine the expected SHA" >&2; exit 1; }

echo "expecting ${EXPECT:0:8}"
printf '%-46s %-10s %-10s %s\n' "REPO" "REVIEW" "@CLAUDE" "FLOOR"

current=0; stale=0; absent=0
pin_state() {  # repo path
  local body
  body=$(gh api "repos/$1/contents/$2" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null) || { echo "-"; return; }
  [ -z "$body" ] && { echo "-"; return; }
  if printf '%s' "$body" | grep -q "$EXPECT"; then echo "ok"
  else printf '%s' "$body" | grep -oE '@[0-9a-f]{8}' | head -1 | tr -d '@' || echo "stale"; fi
}

while IFS=$'\t' read -r repo _branch _protected; do
  [ -z "$repo" ] && continue
  r=$(pin_state "$repo" ".github/workflows/claude-code-review.yml")
  i=$(pin_state "$repo" ".github/workflows/claude.yml")
  f=$(pin_state "$repo" ".github/workflows/floor.yml")
  printf '%-46s %-10s %-10s %s\n' "$repo" "$r" "$i" "$f"
  for v in "$r" "$i" "$f"; do
    case "$v" in ok) current=$((current+1));; -) absent=$((absent+1));; *) stale=$((stale+1));; esac
  done
done < <("$DIR/scope.sh")

echo
echo "-- $current at ${EXPECT:0:8}, $stale stale, $absent missing the workflow"
