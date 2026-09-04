#!/usr/bin/env bash
# sweep.sh — point the fleet's Claude workflows and CI floor at these reusables.
#
# Sweeps three callers into every in-scope repo: the interactive @claude handler
# (claude.yml), the auto-reviewer (claude-code-review.yml), and the Tier 0 CI
# floor (floor.yml, a full-history gitleaks scan).
#
# MASS WRITE. Run by Mark via `!` — the classifier blocks agent-run fleet writes.
# Idempotent: a caller already at the stub's pinned SHA is skipped, so a re-pin
# updates and a current repo costs one read. Direct-commits on unprotected
# default branches; opens a PR where the branch is protected.
#
# Usage:  DRY=1 scripts/fleet-sweep/sweep.sh    # preview, writes NOTHING
#         scripts/fleet-sweep/sweep.sh          # execute
#
# Scope comes from scope.sh (derived from owners.txt, minus exclude.txt) unless
# INSCOPE names a TSV. Nothing is committed: see scope.sh for why.
set -euo pipefail
DRY=${DRY:-0}

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
HOST_REPO="mriechers/github-actions"

STUB_I="$DIR/stubs/claude.yml"
STUB_R="$DIR/stubs/claude-code-review.yml"
STUB_F="$DIR/stubs/floor.yml"
INTERACTIVE=".github/workflows/claude.yml"
REVIEW=".github/workflows/claude-code-review.yml"
FLOOR=".github/workflows/floor.yml"

# Machine is resolved at run time, not baked in. This trailer is written to
# every repo the sweep touches, so a hard-coded hostname would put a false
# provenance claim in ~250 commits the moment anyone ran it from another box.
MSG="chore: use central reusable Claude workflows + CI floor

Points this repo's Claude workflows and secrets floor at the shared
mriechers/github-actions reusables, SHA-pinned.

Agent: fleet-sweep
Machine: $(hostname -s)
Co-Authored-By: Claude <noreply@anthropic.com>"

# ── Preflight ──────────────────────────────────────────────────────────────
# The predecessor compared each stub byte-for-byte against the-lodge's own
# workflow of the same name. That check cannot survive the move: this repo's
# floor.yml IS the reusable, not a caller, so the names collide. It was also
# the wrong check — it proved the stubs matched one consumer, not that they
# pinned anything current. STUB_I was never checked for currency at all, which
# is how the interactive pin sat 21 commits stale fleet-wide (#37) while the
# preflight passed.
#
# What replaces it: the stubs must agree on one SHA, that SHA must exist here
# and be reachable from main, it must carry every change made to the reusables,
# and each stub must name a reusable that actually exists at it.

pin_of() { grep -oE '[0-9a-f]{40}' "$1" | head -1 || true; }

PIN_I=$(pin_of "$STUB_I"); PIN_R=$(pin_of "$STUB_R"); PIN_F=$(pin_of "$STUB_F")
for pair in "claude.yml:$PIN_I" "claude-code-review.yml:$PIN_R" "floor.yml:$PIN_F"; do
  [ -n "${pair#*:}" ] || { echo "ERROR: no pinned SHA found in stubs/${pair%%:*}"; exit 1; }
done

# All three or none. A stub that disagrees is the #37 failure by construction.
if [ "$PIN_I" != "$PIN_R" ] || [ "$PIN_F" != "$PIN_R" ]; then
  echo "ERROR: stubs disagree on the pinned release —"
  echo "         claude.yml             $PIN_I"
  echo "         claude-code-review.yml $PIN_R"
  echo "         floor.yml              $PIN_F"
  echo "       Re-pin all three to the same commit."
  exit 1
fi
PIN="$PIN_R"

git -C "$REPO_ROOT" cat-file -e "$PIN^{commit}" 2>/dev/null \
  || { echo "ERROR: pinned SHA $PIN is not a commit in this repo. Typo, or a fetch is needed."; exit 1; }
git -C "$REPO_ROOT" merge-base --is-ancestor "$PIN" main 2>/dev/null \
  || { echo "ERROR: pinned SHA $PIN is not reachable from main — consumers would pin an orphan."; exit 1; }

# Each stub must call a reusable that exists at the pin. Catches a renamed or
# deleted reusable, which byte-identity never could.
for stub in "$STUB_I" "$STUB_R" "$STUB_F"; do
  path=$(grep -oE 'uses:[[:space:]]*[^@]+@' "$stub" | head -1 | sed -E 's|uses:[[:space:]]*[^/]+/[^/]+/||; s|@$||')
  [ -n "$path" ] || { echo "ERROR: could not parse the uses: line in $stub"; exit 1; }
  git -C "$REPO_ROOT" cat-file -e "$PIN:$path" 2>/dev/null \
    || { echo "ERROR: $(basename "$stub") calls $path, which does not exist at $PIN"; exit 1; }
done

# Currency. The old rule was "the pin must equal main's tip", which a docs-only
# commit broke — and now that the sweep lives beside the reusables, editing this
# very script would break it too. The rule that actually matters is narrower:
# the pin must carry every change made to the reusables. Docs and tooling
# commits move main without invalidating a pin, and that is correct.
NEWEST=$(git -C "$REPO_ROOT" log -1 --format=%H main -- \
  .github/workflows/claude-review.yml \
  .github/workflows/claude-interactive.yml \
  .github/workflows/floor.yml)
if [ "${ALLOW_STALE_PIN:-0}" = "1" ]; then
  echo "NOTE: ALLOW_STALE_PIN=1 — currency check skipped; stubs pin ${PIN:0:8}"
elif ! git -C "$REPO_ROOT" merge-base --is-ancestor "$NEWEST" "$PIN" 2>/dev/null; then
  echo "ERROR: stubs pin ${PIN:0:8}, which predates ${NEWEST:0:8} — the newest commit to a reusable."
  echo "       The fleet would inherit a reusable missing that change."
  echo "       Re-pin all three stubs, or set ALLOW_STALE_PIN=1 for a deliberate rollback."
  exit 1
fi

# A stale checkout silently validates the pin against yesterday's main.
if git -C "$REPO_ROOT" rev-parse --verify -q origin/main >/dev/null 2>&1; then
  git -C "$REPO_ROOT" merge-base --is-ancestor origin/main main 2>/dev/null \
    || echo "WARNING: local main is behind origin/main — 'git pull' before trusting the currency check."
fi

echo "preflight ok — stubs pin ${PIN:0:8}, carrying reusables through ${NEWEST:0:8}"

# ── Scope ──────────────────────────────────────────────────────────────────
SCOPE_TMP=""
if [ -n "${INSCOPE:-}" ]; then
  [ -f "$INSCOPE" ] || { echo "ERROR: INSCOPE=$INSCOPE not found"; exit 1; }
  SCOPE_FILE="$INSCOPE"
  echo "scope: $INSCOPE (override)"
else
  SCOPE_TMP=$(mktemp); trap 'rm -f "$SCOPE_TMP"' EXIT
  echo "scope: deriving from $DIR/owners.txt…"
  "$DIR/scope.sh" > "$SCOPE_TMP"
  SCOPE_FILE="$SCOPE_TMP"
fi
echo "scope: $(wc -l < "$SCOPE_FILE" | tr -d ' ') repos"

put_file() {  # repo path localfile branch
  local repo="$1" path="$2" src="$3" branch="$4" sha content ref
  # Skip only when the remote already references the SAME pinned SHA, so a
  # re-pin updates instead of being skipped.
  ref=$(pin_of "$src")
  if [ -n "$ref" ] && gh api "repos/$repo/contents/$path?ref=$branch" --jq '.content' 2>/dev/null \
       | base64 -d 2>/dev/null | grep -q "$ref"; then
    echo "  = $path already at ${ref:0:8}, skip"; return 0
  fi
  if [ "$DRY" = 1 ]; then echo "  DRY would PUT $path on $branch"; return 0; fi
  content=$(base64 < "$src" | tr -d '\n')
  # This gh leaks 404/403 JSON to stdout; branch on exit status, not output.
  sha=""; if out=$(gh api "repos/$repo/contents/$path?ref=$branch" --jq '.sha' 2>/dev/null); then sha="$out"; fi
  # set -e is suppressed inside process_repo (it runs as an `if (...)`
  # condition), so a failed PUT must be caught here rather than abort.
  if err=$(gh api --method PUT "repos/$repo/contents/$path" \
       -f message="$MSG" -f content="$content" -f branch="$branch" \
       ${sha:+-f sha="$sha"} 2>&1 >/dev/null); then
    echo "  ✓ PUT $path on $branch"
  else
    echo "  ✗ FAILED PUT $path on $branch — $(printf '%s' "$err" | tr '\n' ' ' | head -c 140)"
    return 1
  fi
}

process_repo() {  # repo branch protected
  local repo="$1" branch="$2" protected="$3" br base mkerr rc=0
  if [ "$protected" = yes ]; then
    br="chore/reusable-claude-workflows"
    if [ "$DRY" = 1 ]; then
      echo "  DRY would create branch $br and open a PR to $branch"
    else
      # An unresolved base or a swallowed creation error used to surface three
      # steps later as an opaque PUT failure — or, worse, silently write onto
      # whatever $br already pointed at. Both cases are named here instead.
      if ! base=$(gh api "repos/$repo/git/ref/heads/$branch" --jq '.object.sha' 2>/dev/null) || [ -z "$base" ]; then
        echo "  ✗ could not resolve base branch '$branch' — skipping repo"
        return 1
      fi
      if gh api "repos/$repo/git/ref/heads/$br" >/dev/null 2>&1; then
        # Left as-is rather than force-reset: this branch usually backs an open
        # PR from a prior release, and discarding its history is not the
        # sweep's call to make.
        echo "  ~ $br already exists — writing onto it (prior sweep's branch)"
      elif ! mkerr=$(gh api --method POST "repos/$repo/git/refs" \
             -f ref="refs/heads/$br" -f sha="$base" 2>&1 >/dev/null); then
        echo "  ✗ could not create $br — $(printf '%s' "$mkerr" | tr '\n' ' ' | head -c 120)"
        return 1
      fi
    fi
    put_file "$repo" "$INTERACTIVE" "$STUB_I" "$br" || rc=1
    put_file "$repo" "$REVIEW" "$STUB_R" "$br" || rc=1
    put_file "$repo" "$FLOOR" "$STUB_F" "$br" || rc=1
    if [ "$DRY" != 1 ]; then
      gh pr create --repo "$repo" --base "$branch" --head "$br" \
        --title "chore: use central reusable Claude workflows + CI floor" \
        --body "Point the interactive @claude and Claude review workflows at the shared mriechers/github-actions reusables (SHA-pinned), and add the Tier 0 CI floor (full-history gitleaks scan) from the same release." 2>/dev/null \
        || echo "  (PR exists or none needed)"
    fi
  else
    put_file "$repo" "$INTERACTIVE" "$STUB_I" "$branch" || rc=1
    put_file "$repo" "$REVIEW" "$STUB_R" "$branch" || rc=1
    put_file "$repo" "$FLOOR" "$STUB_F" "$branch" || rc=1
  fi
  return $rc
}

# Per-repo isolation: one repo's failure is tallied, not fatal (set -euo
# pipefail aborts only the subshell). `|| [ -n "$repo" ]` keeps a final row
# that lacks a trailing newline.
ok=0; failed=0; skipped=0; failed_repos=""
while IFS=$'\t' read -r repo branch protected || [ -n "$repo" ]; do
  [ -z "$repo" ] && continue
  # Refused independently of exclude.txt. The stub filenames collide with the
  # reusables this repo hosts — sweeping it would overwrite floor.yml with a
  # caller that calls itself.
  if [ "$repo" = "$HOST_REPO" ]; then
    echo "→ $repo — REFUSED: the host cannot be swept (stub names collide with the reusables)"
    skipped=$((skipped + 1)); continue
  fi
  echo "→ $repo (protected=$protected)"
  if ( process_repo "$repo" "$branch" "$protected" ); then
    ok=$((ok + 1))
  else
    failed=$((failed + 1)); failed_repos="$failed_repos $repo"
    echo "  ✗ FAILED: $repo (continuing)"
  fi
done < "$SCOPE_FILE"

mode=""; [ "$DRY" = 1 ] && mode="(DRY) "
echo "── sweep ${mode}summary: $ok ok, $failed failed, $skipped refused.${failed_repos:+ Failed:$failed_repos}"
