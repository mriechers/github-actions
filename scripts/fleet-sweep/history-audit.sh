#!/usr/bin/env bash
# history-audit.sh — which in-scope repos carry secrets in their git history?
# (read-only)
#
# The gap this closes: the floor scans the commits a change adds, not all
# history. That is what makes a red floor mean "this change added a secret",
# but it also means a secret committed before the floor arrived, or removed in
# a later commit, is never reported by CI. A secret that was pushed is
# published whether or not it is still in the tree. This is the deliberate,
# one-off look at all of it.
#
# Run it once per fleet, and again when an owner is added. Then triage each
# row: rotate the credential if it is or ever was live; otherwise allowlist it
# in that repo's root .gitleaks.toml (see the notes at the top of
# .github/workflows/floor.yml — prefer a path allowlist to fingerprints).
#
# Read-only on purpose. It clones each repo into a temporary directory, scans
# it, prints one row per (rule, file), and deletes the clone. It writes to no
# repo and never prints a secret: gitleaks runs with --redact and only the
# rule, path, commit and date are read from its report.
#
# Each repo's own root .gitleaks.toml and .gitleaksignore apply, exactly as
# they do in the floor, so a finding already triaged does not come back.
#
# The output names private repos. Keep it in the terminal; do not commit it.
#
# Usage:  scripts/fleet-sweep/history-audit.sh                 # whole scope
#         ONLY='owner/repo|owner/other' scripts/fleet-sweep/history-audit.sh
#         FORMAT=tsv scripts/fleet-sweep/history-audit.sh > "$TMPDIR/audit.tsv"
#
# ONLY is an extended regex matched against the whole owner/repo name.
#
# Exit codes:  0 = every repo scanned, no findings
#              1 = findings to triage
#              2 = the audit could not run, or some repos could not be scanned
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
FORMAT="${FORMAT:-table}"
ONLY="${ONLY:-}"

for tool in gh jq gitleaks; do
  command -v "$tool" >/dev/null 2>&1 \
    || { echo "AUDIT FAILED: $tool is not installed." >&2; exit 2; }
done

# FAILING CLOSED, for the same reason as enrollment-audit.sh: a scope that
# cannot be derived must not be reported as a clean fleet.
if ! scope_out=$("$DIR/scope.sh" 2>&1); then
  echo "AUDIT FAILED: scope.sh could not derive the repo list." >&2
  echo "$scope_out" >&2
  exit 2
fi

repos=()
while IFS=$'\t' read -r repo _branch _protected; do
  [ -n "$repo" ] || continue
  if [ -n "$ONLY" ] && ! [[ "$repo" =~ ^(${ONLY})$ ]]; then
    continue
  fi
  repos+=("$repo")
done <<< "$scope_out"

if [ "${#repos[@]}" -eq 0 ]; then
  echo "AUDIT FAILED: no repos to scan (scope empty, or ONLY matched nothing)." >&2
  exit 2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/history-audit.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

rows="$WORK/rows.tsv"
: > "$rows"
clean=0
with_findings=()
unscanned=()

echo "Scanning ${#repos[@]} repo(s)..." >&2
for repo in "${repos[@]}"; do
  clone="$WORK/clone"
  report="$WORK/report.json"
  rm -rf "$clone" "$report"

  if ! gh repo clone "$repo" "$clone" -- --quiet >/dev/null 2>&1; then
    unscanned+=("$repo (clone failed)")
    echo "  ! $repo — clone failed" >&2
    continue
  fi

  # --exit-code 0 so that "found something" and "could not scan" stay
  # distinguishable: any non-zero exit here is a failure to scan.
  if ! gitleaks git "$clone" --redact --no-banner --log-level error \
        --report-format json --report-path "$report" --exit-code 0 \
        >/dev/null 2>"$WORK/gitleaks.err" || [ ! -s "$report" ]; then
    unscanned+=("$repo (gitleaks failed: $(head -c 120 "$WORK/gitleaks.err" | tr '\n' ' '))")
    echo "  ! $repo — scan failed" >&2
    continue
  fi

  # One row per (rule, file): how many findings, and the earliest commit that
  # carries one. Secret, Match and Line are deliberately never read.
  found=$(jq -r --arg repo "$repo" '
      group_by([.RuleID, .File])[]
      | (sort_by(.Date) | .[0]) as $first
      | [$repo, $first.RuleID, $first.File, length,
         ($first.Date | .[0:10]), ($first.Commit | .[0:8])]
      | @tsv' "$report") || {
    unscanned+=("$repo (report unreadable)")
    echo "  ! $repo — report unreadable" >&2
    continue
  }

  if [ -n "$found" ]; then
    printf '%s\n' "$found" >> "$rows"
    with_findings+=("$repo")
    echo "  x $repo — $(printf '%s\n' "$found" | awk -F'\t' '{n+=$4} END {print n}') finding(s)" >&2
  else
    clean=$((clean + 1))
    echo "  . $repo" >&2
  fi
done

header=$'REPO\tRULE\tFILE\tFINDINGS\tFIRST_SEEN\tCOMMIT'
if [ -s "$rows" ]; then
  if [ "$FORMAT" = "tsv" ]; then
    printf '%s\n' "$header"
    cat "$rows"
  else
    { printf '%s\n' "$header"; cat "$rows"; } | column -t -s $'\t'
  fi
fi

{
  echo
  echo "Scanned ${#repos[@]} repo(s): $clean clean, ${#with_findings[@]} with findings, ${#unscanned[@]} not scanned."
  if [ "${#unscanned[@]}" -gt 0 ]; then
    echo "Not scanned — these are unknown, not clean:"
    printf '  %s\n' "${unscanned[@]}"
  fi
} >&2

[ "${#unscanned[@]}" -gt 0 ] && exit 2
[ "${#with_findings[@]}" -gt 0 ] && exit 1
exit 0
