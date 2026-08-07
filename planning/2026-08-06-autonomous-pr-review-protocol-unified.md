# Autonomous PR review protocol — unified specification

**Status:** canonical. This document supersedes, by explicit cross-link:

- `the-lodge/planning/2026-08-05-autonomous-pr-review-protocol.md` (the 105-line
  protocol spec) — its record schema and gates are adopted here with the
  collision resolutions below.
- The Copilot roadmap ("Autonomous PR Review System — Roadmap Adjustment Plan",
  2026-08-05) — its gap analysis and architecture decisions are absorbed;
  its Phase 3 lands in the *worker* contract (`/ship-pr` + `shipwright.md`),
  with `/ship-watch` as the leader it did not know existed.

**Acceptance instrument:**
`the-lodge/planning/2026-08-05-pr-review-loop-edge-cases.md` (23-point
checklist, PR #564). The mapping table at the bottom of this document walks
every item.

The superseded documents remain in place as history; they must not be edited
to match this one. Anything here that contradicts them wins.

---

## System shape

Three skills plus a reusable GitHub Actions workflow coordinate through the
PR itself. The PR is the durable shared state; **labels are the query index;
versioned records in PR comments are the evidence**. Local state files
(`.pr-watch-state.json`, `.ship-pr-state.json`, `.ship-watch-state.json`) are
execution caches and must never be required to recover a PR's state
(`pr_record.rebuild_counters` is the recovery path).

| Component | Role | Home |
|---|---|---|
| `claude-review.yml` | `primary` reviewer (deterministic publisher) | `.github/workflows/` |
| `/pr-watch` | Reviewer leader + `fallback` reviewer | `.claude/skills/pr-watch/` |
| `/ship-pr` | Author worker contract, one PR | `.claude/skills/ship-pr/` |
| `/ship-watch` | Author leader — one persistent shipwright per repo | `.claude/skills/ship-watch/` |

Everything lives in `mriechers/github-actions`. The workflow loads the
protocol scripts from a checkout of this repo pinned to
`github.job_workflow_sha` — the SHA the consumer pinned — so a re-pin moves
the workflow and its taxonomy atomically.

No workflow engine, no new runtime platform, and no runtime dependency on
the-lodge. The human merge checkpoint is the contract: **agents never merge,
never close PRs, never delete remote branches.**

## The taxonomy — one definition, one writer

`.claude/skills/pr-watch/scripts/pr_label.py` `TAXONOMY` is the sole
definition of every label name, color, and description (16 labels). Every
other surface imports or invokes it; a backstop test
(`tests/test_review_publish.py`) fails if the workflow inlines a label that
drifts from it.

Three axes:

- **Axis R — `review:*` (exactly one per PR**, enforced by
  `pr_label.plan_transition`; written only via `pr_label.py set`):
  `new`, `re-review`, `blocker`, `nits`, `approved`, `inconclusive`.
- **Axis S — `ship:*` (at most one per PR**; written with `gh pr edit` after
  `pr_label.py ensure` — `pr_label.py set ship:ready` deliberately exits 2):
  `ready` (terminal-until-revoked), `blocked` (non-terminal, waiting),
  `escalated`, `parked`, `deferred`, `superseded`, `probe` (terminal).
- **Flags (orthogonal, coexist freely):** `claude-fix`, `no-pr-watch`,
  `claude-review`.

Cross-axis rule: a `review:blocker` verdict revokes `ship:ready`
(`REVOKES_SHIP_READY`). `review:blocker` + `ship:blocked` is the canonical
gated state, not a conflict.

**The human's court is one query** (three labels — `gh search prs --label`
cannot express OR; both comma-joins and repeated flags silently return 0):

```
gh api -X GET search/issues \
  -f q='is:pr is:open author:@me label:ship:ready,ship:blocked,ship:escalated'
```

`ship:ready` → merge it. `ship:blocked` → decide it (a reply releases the
shipwright). `ship:escalated` → unstick it. Nothing else belongs in the
user's court.

## Records — `pr-review:v1`

Compact JSON in an HTML comment, invisible in rendered prose, implemented in
`pr_record.py`:

```
<!-- pr-review:v1 {"kind":"review","id":"rvw-...","request_id":"req-...",
"role":"fallback","reviewer":"pr-watch","dispatched_sha":"...",
"reviewed_sha":"...","outcome":"approved","findings":[...],
"reviewed_at":"..."} -->
```

| Kind | Required fields |
|---|---|
| `request` | `id`, `trigger` (`head-change`\|`mention`\|`manual`\|`fallback`), `head_sha`, `requested_at`, `selected_role` (`primary`\|`fallback`) |
| `review` | `id`, `request_id`, `role`, `reviewer`, `dispatched_sha`, `reviewed_sha`, `outcome` (`approved`\|`nits`\|`blocker`\|`inconclusive`), `findings`, `reviewed_at` — optional `ci_state`, `base_state` |
| `disposition` | `id`, `finding_id`, `status`, `evidence` (non-empty), `recorded_at` |
| `terminal` | `id`, `state` (`ready`\|`escalated`\|`parked`\|`deferred`\|`superseded`\|`probe`), `reason`, `recorded_at` |

Roles: `primary` (the Action), `fallback` (`/pr-watch`), `advisory`
(Copilot et al. — can never satisfy a request, write a verdict, contribute
open findings, or reset progress), and `author` (disposition writer). Role
lives in the record, not in prose: the same GitHub login writing an author
note and a genuine review (observed on the-lodge #511) is disambiguated by
`role`, mechanically.

Malformed, unsupported-version, or duplicate-id records are ignored for
state selection and surfaced as protocol errors. Legacy
`<!-- pr-watch: sha=... -->` markers remain readable during a bounded
migration window (until no open PR relies on them); updated writers emit
only v1.

### Who reads and writes what

| Participant | Reads | Writes |
|---|---|---|
| Action publisher | changed-file list, trigger event | `request` (`req-gha-<run_id>`, synthesized) + `review` (role=`primary`) embedded in the formal review body; `review:*` label via `pr_label.py`; check run |
| pr-watch Leader | labels + newest reviewed SHA (`pr_scan.py`) | `request` records on dispatch; `review:re-review` label |
| pr-watch reviewer | diff, prior review/disposition records | `review` (role=`fallback`); verdict label via `pr_label.py set --from-severities` |
| ship-watch Leader | labels only (one `gh search` per tick) | `ship:blocked` add/remove; on inline clears: `ship:ready` label + `terminal` (state=`ready`) |
| shipwright / `/ship-pr` | review records + open-finding set, labels | `disposition`, `request` (`mention`/`manual`), `terminal`; `ship:*` labels |

Leaders never parse records in their tick loop — routing is labels-only,
preserving the one-search-per-tick cost model. A Leader reading a diff is
the regression.

## Review authority and currency

The Action is the `primary` reviewer when a request selects it. `/pr-watch`
is the `fallback` only after a bounded primary timeout or when a request
explicitly selects fallback. The selected role for a request is the **sole
verdict writer**; a concurrent review from any other role stays evidence.

A review is **current** when its request id is the latest request and its
`reviewed_sha` equals the live head. Workers re-resolve the head at start;
`reviewed_sha != dispatched_sha` is expected and valid — a leader treating
that drift as "didn't review" respawns forever. Verdicts come from labels
and records, never `reviewDecision` (empty fleet-wide).

Re-review requests deduplicate by **request id, never SHA** — a
mention-triggered re-review of an unchanged head is a new request (the-lodge
#511 sat four days because SHA-keyed dedup suppressed exactly this).

## Findings lifecycle

Findings are `{id, severity, path, summary, status}` with reviewer-assigned
stable slug ids (`pr:<n>:<slug>`) reused across rounds. Status:
`open`, `fixed`, `contested`, `deferred`, `superseded`, `redesign`.

The **open set is a set difference** (`pr_record.open_findings`): a
verdict-role review recording a finding `open` (re-)opens it; it leaves only
through a `disposition` record carrying evidence. Findings do not decrease
monotonically — fixes generate findings, and the open set may grow round
over round.

**Diff scoping (mechanical, both reviewer paths):** every blocking
(High/Medium) finding must cite a path in the PR's changed-file set
(`gh pr diff --name-only`). Out-of-diff observations demote to non-blocking
follow-ups (`pr_record.validate_blocking_paths`), and an *unobtainable* diff
demotes the verdict to `inconclusive` — never a block against the whole
repository (the-lodge #554: a one-line SHA repin drew a "live secret"
blocker describing 246,527 insertions after a shallow clone fell back to
the empty tree). Review output redacts secrets rather than quoting them.

## `review:inconclusive`

The reviewer could not establish a verdict (no diff obtainable, sandbox
blocked — observed on #557/#552). Semantics:

- Permits bounded retry: **3 attempts total per request id**
  (`pr_record.can_retry`; the Action path enforces the same bound in
  `review_publish.decide_eligibility` by counting completed
  `Review: inconclusive` check runs for the head).
- Does **not** increment the author round counter.
- Does **not** reset the no-progress timer.
- Publishes as: label `review:inconclusive`, review event `COMMENT`, check
  conclusion `neutral`, title `Review: inconclusive`.

## Readiness and terminals

`ship:ready` requires **all** of (`pr_record.ship_ready_eligible`):

1. a current, selected `approved` review with **no open findings**;
2. `ci_state = green`, or `failing-attributed` backed by per-failure
   reproduction/attribution evidence;
3. `base_state = fresh` and no merge conflict; and
4. no terminal record.

CI is five-valued (`pr_record.classify_ci`): `green`,
`failing-attributed`, `failing-unattributed`, `unavailable`, `absent`.
**An empty check list is `absent`, never green** — `all([])` is `True` and
4 of 9 actionable repos have no CI.

New commits revoke `ship:ready`. Security/process hard stops exit directly
to `ship:escalated` — they do not enter the fix loop. Redesign-class
findings park the PR (`ship:parked`, converted to draft with a resume note).
`ship:superseded` and `ship:probe` represent the ~20% of real outcomes that
end closed-not-merged. `ship:blocked` is **not** terminal: an agent triaged
the PR, posted its plan, and resumes the moment the human decides.

## Stop conditions

- **Contested loop:** `pushback_only_rounds ≥ 2` (rebuildable via
  `pr_record.rebuild_counters`) → stop and escalate.
- **No progress:** conclusive verdict-role reviews, requests, dispositions,
  and terminals reset the timer; inconclusive and advisory reviews do not.
  Timeout → `ship:escalated`.
- **Budget:** the dominant historical cost was idle cache-write tax on
  parent `/loop` sessions (~$1,381 in two days), not review work. Leader
  ticks are one `gh search` + roster diff at a 15–20 min heartbeat; Sonnet
  workers; ≤ 8 combined live agents (the ~8–9 fork ceiling is a hard
  machine limit). `usage_report.py --mode fleet` (merged the-lodge #548)
  denominates a dollar ceiling; at the ceiling the Leader escalates
  in-flight work and stops.

## Operating runbook

Two `/loop` sessions, never sharing one session (`/loop` supersedes pending
wakeups): `/loop pr-watch` (reviewer side) and `/loop ship-watch` (author
side). The human's day is the three-label court query above. Kill switches:
interrupt the local loops; `pause-claude-actions.sh` (human-run via `!`)
for the Action, because SHA-pinned callers never see a fix commit. Rollback
is a fleet re-pin to the prior good SHA, never a revert here. Release and
re-pin procedure: `/release-reusable` skill.

---

## Acceptance mapping — the 23-point checklist

Named behavior + test/trace for every item in
`2026-08-05-pr-review-loop-edge-cases.md`:

| # | Property | Named behavior | Test / trace |
|---|---|---|---|
| 1 | Fast path exists | Leader inline-clear of `review:approved` (no agent, no clone) | ship-watch trace (9 PRs cleared 2026-08-04); `test_ship_scan.py` clear-only tests |
| 2 | Findings are addressable objects | stable finding ids in `review` records | `test_pr_record.TestOpenFindings` |
| 3 | Re-review without a new commit | `request` record, `trigger=mention`/`manual` | `test_request_dedup_is_by_id_not_sha` |
| 4 | Verdict and CI separate axes | `ci_state` independent field; `ship_ready_eligible` | `TestShipReady` (per-gate) |
| 5 | Round state survives context loss | `rebuild_counters(records)` | `test_counters_survive_context_loss` |
| 6 | "Redesign, not fix" classification | finding `status=redesign` → `terminal state=parked` | `TestTerminals`; disposition validation |
| 7 | Three-valued triage | disposition `status ∈ {fixed, contested, deferred}` | `TestRebuildCounters` |
| 8 | Liveness timeout | `no_progress()` → `ship:escalated` | `TestInconclusive` timer tests |
| 9 | Base re-validated each round | `base_state` recorded per round (`/ship-pr` step 1) | pilot trace |
| 10 | A severity class exits the loop | hard stops exit directly to `ship:escalated` | spec rule + `TestTerminals` |
| 11 | Every path terminates queryably | Axis S labels + `terminal_state()` | `TestTerminals` |
| 12 | Never-merge pre-flight | `/ship-pr` pre-flight gate; publisher never merges | existing behavior, preserved |
| 13 | Findings scoped to the diff | `validate_blocking_paths` in publisher and reviewer prompt | `test_pr554_whole_repo_blocker_downgrades` (regression fixture) |
| 14 | `review:inconclusive` with bounded retry | collision 4 semantics; `can_retry`; `decide_eligibility` | `TestInconclusive`, `TestEligibility` (#557/#552 fixtures) |
| 15 | Verdict never from `reviewDecision` | `verdict_for` reads records/labels only | `TestVerdictSelection`; no code path consults `reviewDecision` |
| 16 | Declared mandates + precedence | roles; selected role sole writer; advisory never satisfies | `test_advisory_review_never_satisfies` |
| 17 | Identity in the marker, not prose | `role` field (`author` vs `fallback`) | `test_author_role_is_distinguishable_from_reviewer` (#511 fixture) |
| 18 | Stable IDs; open set = set difference | `open_findings()` | `test_fixes_generate_findings` |
| 19 | Workers re-resolve head | `reviewed_sha != dispatched_sha` valid | `test_reviewed_sha_may_differ_from_dispatched` (#549 trace) |
| 20 | Mention bypasses SHA dedup | request-id dedup | `test_request_dedup_is_by_id_not_sha` |
| 21 | Budget is a stop condition | fleet-mode spend wired to Leader `--budget` (dollars) | `usage_report.py` tests (the-lodge #548); pilot trace |
| 22 | Empty check list is not green | `classify_ci([]) == "absent"` | `test_empty_check_list_is_absent_not_green` |
| 23 | `superseded` / `probe` terminals | Axis S + `terminal` records | `test_superseded_and_probe_are_representable` |

Items marked *pilot trace* are validated during the canary rollout
(the-lodge first) and recorded here when observed.
