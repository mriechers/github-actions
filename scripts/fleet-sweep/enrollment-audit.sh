#!/usr/bin/env bash
# enrollment-audit.sh — which in-scope repos never got the stubs? (read-only)
#
# The gap this closes: scope.sh enrolls a repo the moment it exists, but the
# stubs only land when sweep.sh runs, and nothing triggers a sweep on repo
# creation. So a new repo is in scope and uninstalled at the same time, and
# stays that way until someone releases something unrelated. It is invisible
# until you notice a consumer has no reviews -- which is how tv-debloat was
# found, four days late and by accident.
#
# Read-only on purpose. This says WHAT is missing; sweep.sh remains the only
# thing that writes, and remains a human-run fleet write.
#
# Why not status.sh: that reports pin currency across all three stubs and costs
# three content reads per repo (~270 calls). Enrollment only asks "does this
# repo have the workflows at all", which is one directory listing per repo.
#
# Usage:  scripts/fleet-sweep/enrollment-audit.sh          # human table
#         FORMAT=summary scripts/fleet-sweep/enrollment-audit.sh
#
# Exit codes:  0 = every in-scope repo is enrolled
#              1 = gaps found (unenrolled or partially enrolled repos)
#              2 = the audit could not run -- see FAILING CLOSED below
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
FORMAT="${FORMAT:-table}"
STUBS=(claude.yml claude-code-review.yml floor.yml)
SWEEP_BRANCH="chore/reusable-claude-workflows"
# The workflows are inert without this. Callers forward their own token
# (CLAUDE.md, "no secrets live here"), and the sweep does not install it.
SECRET="CLAUDE_CODE_OAUTH_TOKEN"

# FAILING CLOSED. An audit that cannot enumerate must not report "no gaps" --
# that is the exact shape of the bug this repo already learned once, when a
# hand-maintained inscope.tsv silently unenrolled 38 repos and looked clean
# doing it. A missing token, a revoked scope or a rate limit all produce an
# empty listing, which is indistinguishable from a healthy fleet unless we
# refuse to interpret it.
if ! scope_out=$("$DIR/scope.sh" 2>&1); then
  echo "AUDIT FAILED: scope.sh could not derive the repo list." >&2
  echo "$scope_out" >&2
  exit 2
fi
scope_count=$(printf '%s\n' "$scope_out" | grep -c . || true)
if [ "$scope_count" -lt 2 ]; then
  echo "AUDIT FAILED: scope resolved to $scope_count repo(s) -- implausible." >&2
  echo "Treating as a credentials or rate-limit failure rather than an empty fleet." >&2
  exit 2
fi

unenrolled=(); partial=(); inert=(); ok=0; unreadable=()

secret_state() {  # repo -> present | absent | unknown
  local repo="$1" out rc
  out=$(gh api "repos/$repo/actions/secrets/$SECRET" 2>&1); rc=$?
  [ $rc -eq 0 ] && { echo present; return; }
  case "$out" in
    *"Not Found"*|*"404"*) echo absent ;;
    # 403 means the token cannot read secrets. That is not evidence of absence,
    # and reporting it as present would be the same fail-open this script
    # exists to refuse.
    *) echo unknown ;;
  esac
}

for_repo() {  # repo -> prints "count/3" or "ERR"
  local repo="$1" listing rc
  listing=$(gh api "repos/$repo/contents/.github/workflows" --jq '.[].name' 2>&1); rc=$?
  if [ $rc -ne 0 ]; then
    # A repo with no .github/workflows directory 404s. That is a real answer
    # (zero stubs), not a failure. Anything else is a failure and must say so
    # rather than be counted as "unenrolled" -- an auth error that reads as a
    # gap would send someone sweeping a repo that is already fine.
    case "$listing" in
      *"Not Found"*|*"404"*) echo "0"; return 0 ;;
      *) echo "ERR"; return 0 ;;
    esac
  fi
  local n=0 s
  for s in "${STUBS[@]}"; do
    printf '%s\n' "$listing" | grep -qxF "$s" && n=$((n+1))
  done
  echo "$n"
}

while IFS=$'\t' read -r repo _branch _protected; do
  [ -z "$repo" ] && continue
  n=$(for_repo "$repo")
  case "$n" in
    ERR) unreadable+=("$repo") ;;
    0)   unenrolled+=("$repo") ;;
    3)   # Stubs alone are not enrollment. A repo with all three workflows and
         # no token has a reviewer that errors on every run -- which reads as a
         # broken review rather than a missing credential, and cost three PRs
         # on tv-debloat exactly that confusion.
         case "$(secret_state "$repo")" in
           present) ok=$((ok+1)) ;;
           absent)  inert+=("$repo") ;;
           *)       unreadable+=("$repo (secret unreadable)") ;;
         esac ;;
    *)   partial+=("$repo ($n/3)") ;;
  esac
done < <(printf '%s\n' "$scope_out")

# Stranded sweep PRs. The sweep opens a PR instead of pushing wherever the
# default branch is protected, and those do not merge themselves: three sat
# open for five days before anyone looked. Automating enrollment makes MORE of
# them, so the audit that justifies the automation has to count them too.
stranded=$(gh api -X GET search/issues \
  -f q="is:pr is:open head:$SWEEP_BRANCH" \
  --jq '.items[] | "\(.repository_url | split("/") | .[-2:] | join("/"))#\(.number)  \(.created_at[:10])"' \
  2>/dev/null || true)

gaps=$(( ${#unenrolled[@]} + ${#partial[@]} + ${#inert[@]} ))

if [ "$FORMAT" != summary ]; then
  echo "scope: $scope_count repos"
  echo
  if [ ${#unenrolled[@]} -gt 0 ]; then
    echo "UNENROLLED — none of the three workflows (${#unenrolled[@]}):"
    printf '  %s\n' "${unenrolled[@]}"; echo
  fi
  if [ ${#partial[@]} -gt 0 ]; then
    echo "PARTIAL — some stubs missing (${#partial[@]}):"
    printf '  %s\n' "${partial[@]}"; echo
  fi
  if [ ${#inert[@]} -gt 0 ]; then
    echo "INERT — all three workflows present, but no $SECRET (${#inert[@]}):"
    printf '  %s\n' "${inert[@]}"
    echo "  These fail on every run. Set the secret; the sweep does not."; echo
  fi
  if [ -n "$stranded" ]; then
    echo "STRANDED sweep PRs — open, awaiting a merge:"
    printf '%s\n' "$stranded" | sed 's/^/  /'; echo
  fi
  if [ ${#unreadable[@]} -gt 0 ]; then
    echo "UNREADABLE — could not be checked (${#unreadable[@]}):"
    printf '  %s\n' "${unreadable[@]}"; echo
  fi
fi

echo "-- $ok working, ${#unenrolled[@]} unenrolled, ${#partial[@]} partial, ${#inert[@]} inert, ${#unreadable[@]} unreadable"

# An unreadable repo is not a clean result. Report gaps if anything is wrong.
[ $gaps -eq 0 ] && [ ${#unreadable[@]} -eq 0 ] && exit 0
exit 1
