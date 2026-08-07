# PR review unification — collision decisions, migration, dispositions

Companion to `2026-08-06-autonomous-pr-review-protocol-unified.md` (the
canonical protocol spec). This document records *why* the four collisions
resolved the way they did, the migration sequence, and the disposition of
every in-flight PR. Kickoff:
`the-lodge/planning/expanded-prompt-autonomous-pr-review-unification.md`.

## The four collisions

### 1 — Leader/worker protocol responsibility

**Decision: workers own records; leaders stay label-index-only.**

Neither source document knew `/ship-watch` existed. The protocol lands in
the participants that already read PR content: the Action publisher and the
pr-watch reviewer write `review` records; the shipwright (running the
`/ship-pr` contract) writes `disposition`/`terminal` records. Leaders route
on labels alone — one `gh search` + roster diff per tick — with exactly two
narrow writes for the ship-watch Leader (`ship:blocked` add/remove, and
`ship:ready` + a `terminal state=ready` record on inline clears).

*Rationale:* preserves the measured cost model (the $1,381/2-day incident
was idle leader tax, and "a Leader reading a diff is the regression" is
ship-watch's own design rule) and the 8-agent machine ceiling. The Copilot
plan's Phase 3 ("rework /ship-pr as an evidence-driven author loop") is
correct but described the worker without the orchestrator — its content
lands in `/ship-pr` + `prompts/shipwright.md`, unchanged in role.

### 2 — `ship:blocked` vs the spec's six terminals

**Decision: `ship:blocked` survives as a non-terminal waiting state; the
`ship:*` axis becomes a seven-value mutually-exclusive disposition axis.**

- Non-terminal: `blocked` (agent triaged, plan posted, resumes on release).
- Terminal-until-revoked: `ready`.
- Terminal: `escalated`, `parked`, `deferred`, `superseded`, `probe`.

*Rationale:* blocked ≠ escalated. A blocked PR has an assigned agent waiting
on a routine go/no-go and resumes automatically on release;
an escalated PR's loop stopped abnormally and nothing resumes without human
intervention. Folding blocked into escalated destroys the release path and
the re-dispatch guard (`classify()` checks `awaiting_user` before `assign`
precisely so gated PRs are not re-dispatched every tick). Removing it
recreates the triaged-vs-untouched indistinguishability its GOTCHA
documents. The five `test_ship_scan.py` precedence tests survive unchanged.

**Consequence — the human-court query grows to three labels**
(`label:ship:ready,ship:blocked,ship:escalated`): escalated PRs genuinely
wait on a human, and leaving them out reintroduces the silently-empty-queue
failure. *This changes the user's documented saved search* — flagged for
veto; reverting means a separate escalation surface.

### 3 — Seven taxonomy definition sites → one

**Decision: `pr_label.py` `TAXONOMY` (now in this repo) is the sole
definition; every other site imports or invokes it.**

| Former site | Now |
|---|---|
| `pr_label.py` (the-lodge) | Moved here; grown to 16 labels |
| `feat/ship-watch` copy (+`ship:blocked`) | Absorbed |
| `ship_scan.py` hand-mirrored constants | `from pr_label import …` (sibling-skill path) |
| `ship-pr` SKILL.md `gh label create ship:ready --color 0e8a16` fallback | Deleted — `ensure` + `gh pr edit`; the fallback's color/description were a fourth, conflicting definition |
| `claude-review.yml` shell label block + heredoc maps | `pr_label.py set` + `review_publish.py`; backstop test asserts any residual inline strings match |
| `migrate_labels.sh` | Ported as legacy rename map; targets asserted ∈ TAXONOMY |
| Protocol spec label lists | Informative only; canonical is the code |

### 4 — `REVIEW_STATES` 5 → 6 (`review:inconclusive`)

**Decision: added**, color `d4c5f9`. Bounded retry (3 per request id), no
round increment, no timer reset. The Action's structured-output enum gains
`inconclusive` → COMMENT + `neutral` check titled `Review: inconclusive`;
publication eligibility skips only on a *conclusive* completed check and
allows retries under the bound. Prerequisite landed first: label-count
tests derive from `len(TAXONOMY)` with one intentional exact-name-set
assertion (the literal-`9` de-hardcoding lesson from `feat/ship-watch`,
finished rather than repeated).

Note: the kickoff described #549's `review:blocker` as possibly stale; per
the taxonomy's own rule it is correctly backed (an open Medium finding, and
Medium ∈ `BLOCKING_SEVERITIES`).

## Migration

Repo layout after migration: `planning/`, `scripts/` (workflow-facing
publisher), `tests/`, `.claude/skills/{pr-watch,ship-pr,ship-watch}/`
(each with its own `scripts/`+`tests/` exactly as deployed), `CLAUDE.md`
etc. from PR #5. **Stays in the-lodge:** `/start`, `/wrap-up` (label
*consumers*), `scripts/claude-workflow-migration/` (fleet-operator tooling:
`sweep.sh`, `pause-claude-actions.sh`, stubs), registry.yaml + lodge-doctor
+ `sync_user_skills.sh` (the rail itself), and `COMMIT_CONVENTIONS.md`
(this repo's `CLAUDE.md` carries its own trailer rules).

Sequence (A→D; [H] = human-only):

- **A — PR hygiene** (done): GA#5 trimmed; GA#4 repinned to
  add-to-project v2; advisory close recommendations on GA#7/GA#2; lodge
  #527 re-grafted onto main. [H] merge #5, #527, #564; close #7, #2.
- **B — build the new home** (this branch): protocol library + tests + CI +
  workflow consumption + skills port + this spec. [H] merge; then close
  lodge #549 as superseded-by-port (both open findings fixed in the port).
- **C — flip the rail** (one lodge PR, atomic): add
  `$HOME/Developer/github-actions/.claude/skills` to `sync_user_skills.sh`
  `GLOBAL_SOURCES`; registry.yaml gains a `github-actions` `projects:` root,
  `canonical:` repoints for pr-watch/ship-pr, and a ship-watch entry;
  **delete** `the-lodge/.claude/skills/project/{pr-watch,ship-pr}`;
  cross-link stubs in lodge planning. Preconditions: both primary checkouts
  on current main ([H] for the-lodge's, which sits on a feature branch).
  After [H] merge: run `sync_user_skills.sh` (dry-run first), verify
  `~/.claude/skills/{pr-watch,ship-pr,ship-watch}` resolve into
  github-actions, lodge-doctor clean.
- **D — fleet propagation**: [H] canary re-pin the-lodge's caller to the
  new SHA; one live review validates `github.job_workflow_sha`, the App
  path, and `pr_label.py` writes under the Action token (fallback if
  `job_workflow_sha` is unpopulated: the `protocol_ref` input). Recreate
  `stubs/claude-code-review.yml` + re-teach `sweep.sh` to write it (the
  stub was deleted 2026-07-31, so the review workflow currently has no
  propagation rail). [H] `DRY=1 sweep.sh` then real via `!`; optional
  `v1` retag. Reap the three merged copilot worktrees.

## In-flight PR dispositions

| PR | Disposition | Actor |
|---|---|---|
| lodge #549 (`feat/ship-watch`) | **Superseded by the B-phase port** (both open findings fixed: shipwright.md no longer posts `@claude please review`; `needs_clone`/`agents_required` no longer over-count clear-only repos). Close, do not merge | [H] close |
| lodge #548 (cost accounting) | Already merged; scripts ported with pr-watch | — |
| lodge #527 (label readers) | Re-grafted onto main (head `cfdc939`), both nits fixed; merge in the-lodge — `/start`//`/wrap-up` stay there | [H] merge |
| lodge #564 (edge cases + old spec) | Merge as history; superseded by cross-link, not edit | [H] merge |
| GA #7 (stale duplicate) | Close without merge (advisory comment posted) | [H] close |
| GA #5 (repo doctrine) | Merge (trims pushed: `enabledPlugins` removed, hook claims corrected) | [H] merge |
| GA #4 (add-to-project) | Repinned to v2 SHA; orthogonal — merge when convenient | [H] merge |
| GA #2 (hex colors in prompt) | Close — opposite of single-definition (advisory comment posted) | [H] close |
