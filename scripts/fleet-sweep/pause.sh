#!/usr/bin/env bash
# pause.sh — reversibly disable/enable the fleet's server-side Claude workflows.
#
# Does NOT delete anything. Flips each workflow's active state via
# `gh workflow disable`/`enable`, so it is fully reversible. This is the lever
# to reach for when a workflow misbehaves fleet-wide, because callers are
# SHA-pinned: editing the reusable cannot reach them, but disabling can.
#
# MASS operation. Run by Mark via `!` — the classifier blocks agent fleet
# writes. DRY-run by default.
#
# Usage:
#   WORKFLOWS="claude-code-review.yml" pause.sh                    # preview (DRY=1 default)
#   WORKFLOWS="claude-code-review.yml" DRY=0 pause.sh              # pause
#   WORKFLOWS="claude-code-review.yml" DRY=0 MODE=enable pause.sh  # un-pause
#   WORKFLOWS="claude.yml floor.yml" DRY=0 pause.sh                # several at once
#
# WORKFLOWS is REQUIRED, deliberately. The predecessor defaulted to
# claude-code-review.yml and justified it as an inert no-op, because that
# workflow had been retired fleet-wide on 2026-07-30. It was restored on
# 2026-08-06 and the default was never revisited — so a bare run would now
# disable the fleet's auto-reviewer everywhere. A default that changed meaning
# underneath its own comment should not have one.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${MODE:-disable}"
DRY="${DRY:-1}"

if [ -z "${WORKFLOWS:-}" ]; then
  echo "ERROR: WORKFLOWS is required — name the workflow files explicitly." >&2
  echo "       e.g. WORKFLOWS=\"claude-code-review.yml\" DRY=0 $0" >&2
  exit 2
fi
read -r -a WORKFLOW_LIST <<< "$WORKFLOWS"

case "$MODE" in
  disable) WANT=disabled_manually ;;
  enable)  WANT=active ;;
  *) echo "MODE must be disable|enable" >&2; exit 2 ;;
esac

# Same derived scope as the sweep, so pausing and sweeping cannot disagree
# about which repos are the fleet.
printf 'MODE=%s DRY=%s WORKFLOWS="%s"\n\n' "$MODE" "$DRY" "${WORKFLOW_LIST[*]}"
changed=0; already=0; absent=0; failed=0; failed_list=""

while IFS=$'\t' read -r repo _branch _protected; do
  [ -z "$repo" ] && continue
  for wf in "${WORKFLOW_LIST[@]}"; do
    # 404 => the repo does not carry this workflow file; tally and move on.
    info=$(gh api "repos/$repo/actions/workflows/$wf" --jq '[.id,.state]|@tsv' 2>/dev/null) \
      || { absent=$((absent+1)); continue; }
    state=${info##*$'\t'}
    if [ "$state" = "$WANT" ]; then already=$((already+1)); continue; fi
    if [ "$DRY" = 1 ]; then
      printf '  DRY %-44s %-24s %s -> %s\n' "$repo" "$wf" "$state" "$WANT"
      changed=$((changed+1)); continue
    fi
    if gh workflow "$MODE" "$wf" --repo "$repo" >/dev/null 2>&1; then
      printf '  ok  %-44s %-24s %s -> %s\n' "$repo" "$wf" "$state" "$WANT"
      changed=$((changed+1))
    else
      printf '  XX  %-44s %-24s FAILED\n' "$repo" "$wf"
      failed=$((failed+1)); failed_list="$failed_list $repo:$wf"
    fi
  done
done < <("$DIR/scope.sh")

printf '\n-- %s%s summary: %d to-change, %d already-%s, %d absent-workflow, %d failed.%s\n' \
  "$([ "$DRY" = 1 ] && echo '(DRY) ')" "$MODE" "$changed" "$already" "$WANT" "$absent" "$failed" \
  "${failed_list:+ Failed:$failed_list}"
