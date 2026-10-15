#!/usr/bin/env bash
# enroll-one.sh <owner/repo> — prepare one new repo's enrollment (read-only).
#
# The gap: scope.sh puts a new repo in scope the moment it exists, but the stubs
# only land when sweep.sh runs, and nothing runs it on repo creation. Enrolling
# one repo by hand took five steps and two judgment calls the first time
# (2026-10-14). This script does the lookups and writes the one-line scope file;
# it prints the commands, and Mark runs them.
#
# It never writes to GitHub. sweep.sh stays the only writer and stays a
# human-run fleet write (`!`). The only thing this script creates is a local TSV
# under $TMPDIR, passed to sweep.sh as INSCOPE so the sweep touches one repo.
#
# Usage:  scripts/fleet-sweep/enroll-one.sh <owner/repo>
#
# Exit codes:  0 = plan printed (or the repo is already enrolled)
#              1 = refused (the host repo, an excluded repo, a bad name)
#              2 = could not run (gh auth, repo not found, unreadable branch)
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
HOST_REPO="mriechers/github-actions"
SECRET="CLAUDE_CODE_OAUTH_TOKEN"
STUBS=(claude.yml claude-code-review.yml floor.yml)

REPO="${1:-}"
if ! printf '%s\n' "$REPO" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$'; then
  echo "usage: enroll-one.sh <owner/repo>" >&2; exit 1
fi
if [ "$REPO" = "$HOST_REPO" ]; then
  echo "refused: $HOST_REPO hosts the reusables and cannot be swept." >&2; exit 1
fi
# exclude.txt holds globs, matched the way scope.sh matches them.
strip_comments() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' "$1" | grep -v '^$' || true; }
if [ -f "$DIR/exclude.txt" ]; then
  while IFS= read -r pat; do
    # shellcheck disable=SC2053  # glob match is intended
    if [[ "$REPO" == $pat ]]; then
      echo "refused: $REPO matches '$pat' in exclude.txt — it is opted out on purpose." >&2; exit 1
    fi
  done < <(strip_comments "$DIR/exclude.txt")
fi
# A repo whose owner is not in owners.txt is out of scope: the next full sweep would not
# maintain it, so enrolling it by hand leaves a pin nothing updates.
if ! strip_comments "$DIR/owners.txt" | grep -qxF "${REPO%%/*}"; then
  echo "refused: owner ${REPO%%/*} is not in owners.txt, so full sweeps would never re-pin this repo." >&2
  echo "         Add the owner there first if it should be part of the fleet." >&2; exit 1
fi

# ── Facts about the target ────────────────────────────────────────────────
if ! BRANCH=$(gh api "repos/$REPO" --jq .default_branch 2>/dev/null) || [ -z "$BRANCH" ]; then
  echo "could not read $REPO — check the name and your gh auth." >&2; exit 2
fi
if ! PROTECTED=$(gh api "repos/$REPO/branches/$BRANCH" --jq .protected 2>/dev/null); then
  echo "could not read branch $BRANCH on $REPO." >&2; exit 2
fi

have=$(gh api "repos/$REPO/contents/.github/workflows" --jq '.[].name' 2>/dev/null || true)
missing=()
for s in "${STUBS[@]}"; do
  printf '%s\n' "$have" | grep -qxF "$s" || missing+=("$s")
done
if gh secret list -R "$REPO" 2>/dev/null | cut -f1 | grep -qxF "$SECRET"; then has_secret=1; else has_secret=0; fi

echo "repo:      $REPO"
echo "branch:    $BRANCH (protected: $PROTECTED)"
if [ ${#missing[@]} -eq 0 ]; then echo "stubs:     all three present"; else echo "stubs:     missing ${missing[*]}"; fi
if [ "$has_secret" -eq 1 ]; then echo "secret:    $SECRET set"; else echo "secret:    $SECRET MISSING — the workflows would start and then error"; fi

if [ ${#missing[@]} -eq 0 ] && [ "$has_secret" -eq 1 ]; then
  echo
  echo "Already enrolled. status.sh reports whether its pin is current."
  exit 0
fi

# ── Pin currency (the same rule as sweep.sh's check 4) ─────────────────────
pin_of() { grep -oE '[0-9a-f]{40}' "$1" | head -1 || true; }
PIN=$(pin_of "$DIR/stubs/claude-code-review.yml")
REUSABLES=(.github/workflows/claude-review.yml .github/workflows/claude-interactive.yml .github/workflows/floor.yml)
NEWEST=$(git -C "$REPO_ROOT" log -1 --format=%H main -- "${REUSABLES[@]}" 2>/dev/null || true)
STALE=""
if [ -n "$PIN" ] && [ -n "$NEWEST" ] && ! git -C "$REPO_ROOT" merge-base --is-ancestor "$NEWEST" "$PIN" 2>/dev/null; then
  STALE="ALLOW_STALE_PIN=1 "
fi

# ── The scope file ────────────────────────────────────────────────────────
TSV="${TMPDIR:-/tmp}/enroll-$(printf '%s' "$REPO" | tr '/' '-').tsv"
printf '%s\t%s\t%s\n' "$REPO" "$BRANCH" "$PROTECTED" > "$TSV"

echo
echo "Scope file: $TSV"
echo
echo "Run these yourself, from $REPO_ROOT:"
echo
if [ "$has_secret" -eq 0 ]; then
  echo "  ! gh secret set $SECRET -R $REPO --body \"\$(get-secret.sh $SECRET)\""
fi
echo "  ! DRY=1 ${STALE}INSCOPE=$TSV scripts/fleet-sweep/sweep.sh"
echo "  ! ${STALE}INSCOPE=$TSV scripts/fleet-sweep/sweep.sh"
echo
if [ "$PROTECTED" = "true" ]; then
  echo "The branch is protected, so the sweep opens a PR on $REPO instead of committing. Merge it."
else
  echo "The branch is unprotected, so the sweep commits the stubs straight to $BRANCH."
fi

if [ -n "$STALE" ]; then
  echo
  echo "JUDGMENT CALL — the stubs pin ${PIN:0:8}, older than ${NEWEST:0:8}, the newest reusable change."
  echo "ALLOW_STALE_PIN=1 is meant for rollbacks. It is safe here only if every change since the pin"
  echo "is behaviour-free. Read this first:"
  echo
  git -C "$REPO_ROOT" log --format='    %h %s' "$PIN..$NEWEST" -- "${REUSABLES[@]}"
  echo
  echo "If any of those change behaviour, do not enroll on the old pin: re-pin the fleet first"
  echo "(the Releasing steps in CLAUDE.md)."
fi

open_prs=$(gh pr list -R "$REPO" --state open --json number --jq '[.[].number] | map(tostring) | join(" ")' 2>/dev/null || true)
if [ -n "$open_prs" ]; then
  echo
  echo "Open PRs ($open_prs) will not be reviewed until their branch includes the new stubs. After the sweep,"
  echo "for each one: gh pr update-branch <n> -R $REPO  (a server-side merge), then remove and re-add"
  echo "the claude-review label."
fi
echo
echo "Expect one false finding: the reviewer restores .claude/, CLAUDE.md and .mcp.json from"
echo "$BRANCH (the PR head is untrusted), so a PR that changes those can read as reverting them."
