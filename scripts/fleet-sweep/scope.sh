#!/usr/bin/env bash
# scope.sh — derive the sweep's in-scope list instead of maintaining it by hand.
#
# Emits the TSV sweep.sh consumes:  owner/repo <TAB> default-branch <TAB> protected
#
# Why derived: the predecessor was a committed inscope.tsv, hand-edited. It
# drifted to 49 rows against ~104 live repos, and a whole owner (wpr-ttbook) was
# never in it at all. A hand-written allowlist fails OPEN — a repo missing from
# it is silently unenrolled, which is indistinguishable from a repo deliberately
# left out. Deriving from `gh repo list` inverts that: new repos are in scope the
# moment they exist, and staying out requires saying so in exclude.txt.
#
# It is also why the list is NOT committed. 39 of inscope.tsv's 49 rows named
# PRIVATE repos; this repo is public, and a private-repo inventory published here
# could not be unpublished. Deriving it at runtime under the operator's own `gh`
# auth keeps it out of git entirely.
#
# Usage:  scripts/fleet-sweep/scope.sh              # print the TSV
#         INCLUDE_FORKS=1 scripts/fleet-sweep/scope.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OWNERS_FILE="${OWNERS_FILE:-$DIR/owners.txt}"
EXCLUDE_FILE="${EXCLUDE_FILE:-$DIR/exclude.txt}"
INCLUDE_FORKS="${INCLUDE_FORKS:-0}"
HOST_REPO="mriechers/github-actions"

[ -f "$OWNERS_FILE" ] || { echo "ERROR: $OWNERS_FILE not found" >&2; exit 1; }

strip_comments() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' "$1" | grep -v '^$' || true; }

mapfile -t OWNERS < <(strip_comments "$OWNERS_FILE")
[ "${#OWNERS[@]}" -gt 0 ] || { echo "ERROR: no owners listed in $OWNERS_FILE" >&2; exit 1; }

EXCLUDES=()
[ -f "$EXCLUDE_FILE" ] && mapfile -t EXCLUDES < <(strip_comments "$EXCLUDE_FILE")

excluded() {  # repo
  local repo="$1" pat
  # The host repo is refused here as well as in sweep.sh. Two independent
  # refusals, because the consequence is overwriting a reusable with a stub
  # that calls itself, and one editable file should not be the only guard.
  [ "$repo" = "$HOST_REPO" ] && return 0
  for pat in ${EXCLUDES+"${EXCLUDES[@]}"}; do
    # shellcheck disable=SC2053  # glob match is intended
    [[ "$repo" == $pat ]] && return 0
  done
  return 1
}

for owner in "${OWNERS[@]}"; do
  if ! listing=$(gh repo list "$owner" --no-archived --limit 500 \
        --json nameWithOwner,isFork,defaultBranchRef \
        --jq '.[]|"\(.nameWithOwner)\t\(.isFork)\t\(.defaultBranchRef.name // "")"' 2>/dev/null); then
    echo "ERROR: could not list repos for '$owner' — check auth and the owner name" >&2
    exit 1
  fi

  while IFS=$'\t' read -r repo is_fork branch; do
    [ -n "$repo" ] || continue
    [ "$is_fork" = "true" ] && [ "$INCLUDE_FORKS" != "1" ] && continue
    # An empty repo has no default branch; there is nothing to commit onto.
    [ -n "$branch" ] || continue
    excluded "$repo" && continue

    # Protection decides direct-commit vs open-a-PR. A repo whose protection we
    # cannot read is treated as PROTECTED: the failure mode of guessing wrong
    # that way is an unnecessary PR, and the other way is a blocked direct push
    # or an unreviewed commit onto a branch someone protected deliberately.
    if protected=$(gh api "repos/$repo/branches/$branch" --jq '.protected' 2>/dev/null); then
      [ "$protected" = "true" ] && protected=yes || protected=no
    else
      protected=yes
    fi

    printf '%s\t%s\t%s\n' "$repo" "$branch" "$protected"
  done <<< "$listing"
done
