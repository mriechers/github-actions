#!/usr/bin/env bash
# Rename legacy pr-review label names to the canonical taxonomy, then bootstrap.
# `gh label edit --name` renames IN PLACE, preserving existing PR assignments.
# Idempotent: a rename whose source is already gone is skipped, not an error.
set -euo pipefail

DRY="${DRY:-1}"
REPOS=(
  "mriechers/the-lodge"
  "mriechers/proxmox-config"
  "mriechers/opnsense-config"
  "mriechers/second-brain"
  "mriechers/apple-automator-workflows"
  "mriechers/emulation"
  "public-media-work/cardigan"
  "public-media-work/pbswi"
  "public-media-work/station-analytics"
  "public-media-work/agentic-social-dash"
  "Wonder-Cabinet-Productions/wonder-cabinet-episode"
  "Wonder-Cabinet-Productions/prx-to-ghost-publisher"
)
RENAMES=("review:pending=review:new" "review:ready=review:approved")

run() {
  if [[ "$DRY" != "0" ]]; then echo "DRY: $*"; else "$@"; fi
}

for repo in "${REPOS[@]}"; do
  existing="$(gh label list --repo "$repo" --limit 200 --json name --jq '.[].name')"
  for pair in "${RENAMES[@]}"; do
    old="${pair%%=*}"; new="${pair##*=}"
    if grep -qxF "$old" <<<"$existing"; then
      if grep -qxF "$new" <<<"$existing"; then
        echo "SKIP $repo: both '$old' and '$new' exist — resolve by hand"
      else
        run gh label edit "$old" --repo "$repo" --name "$new"
      fi
    else
      echo "SKIP $repo: '$old' not present"
    fi
  done
  # --reconcile: labels that predate the taxonomy (review:blocker, review:nits,
  # claude-review) carry drifted colors; force-upsert repairs them.
  if [[ "$DRY" != "0" ]]; then
    python3 "$(dirname "$0")/pr_label.py" ensure "$repo" --reconcile --dry-run
  else
    python3 "$(dirname "$0")/pr_label.py" ensure "$repo" --reconcile
  fi
done
