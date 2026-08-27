---
name: ship-watch
description: >
  Author-side team leader — the mirror of /pr-watch. A manually invoked Leader that
  scans your own open PRs carrying reviewer verdicts and assigns each REPO a
  dedicated persistent Sonnet "shipwright" that drives that repo's PRs to ship:ready.
  Nits ship unattended; blockers stop for your approval. Never merges. Triggers on
  "ship watch", "watch my PRs for feedback", "drive my PRs to merge-ready", "work the
  review queue".
---

# Ship Watch — author-side leader

The **author** seat opposite `/pr-watch`. A **manually invoked** Leader scans your own
PRs that a reviewer has already ruled on, and dispatches each **repo** to a persistent
**Sonnet shipwright** that owns that repo and drives its PRs to `ship:ready`. One
invocation is one pass — it does not schedule itself.

`/pr-watch` posts the verdicts. `/ship-watch` consumes them. The two never call each
other — they ping-pong through the PR itself: a shipwright's push moves the head SHA →
`/pr-watch` notices → its reviewer re-reviews → the label flips → `/ship-watch` notices.

**Why per-repo rather than per-PR:** a shipwright loads a repo's `CLAUDE.md`, test and
lint commands, conventions and layout **once**, then reuses them across every PR in that
repo and every review round. That is the context saving. It also makes it structurally
impossible for two agents to fight over one checkout, and it fits the fleet under the
hard agent ceiling (see Guardrails).

**Arguments:** `$ARGUMENTS`

| Arg | Mode |
|---|---|
| (none) | **Single pass** — scan, assign, verify, retire |
| `--repo <owner/repo>` | **Scoped one-shot** — one repo, no team (good for testing) |
| `--preview` | **Dry run** — print the work table; change nothing |
| `--status` | Summarize the roster and in-flight PRs; change nothing |
| `--auto-blockers` | **Modifier** — let `review:blocker` PRs ship unattended (default: gated) |
| `--budget <n>` | **Modifier** — stop after `n` PRs reach `ship:ready` this session |

The scan helper is `scripts/ship_scan.py` (stdlib only).

## Prerequisite: /pr-watch's label helper

`/ship-watch` reads the label taxonomy that `/pr-watch` writes, and writes `ship:ready`
through the same helper so there is exactly one definition of the taxonomy. On the first
pass, resolve `pr_label.py` at its deployed sibling path:

```bash
ls ~/.claude/skills/pr-watch/scripts/pr_label.py
```

If it does not exist, say so and stop — the handshake is not installed (both skills
deploy together from `mriechers/github-actions`). Pass the resolved path to every
shipwright as `label_script`; never let a teammate construct it.

## The handshake — labels, not prose

| Label on the PR | Leader action |
|---|---|
| `review:blocker` | **assign**, `mode: gated` — shipwright triages and posts a plan, then waits for you |
| `review:nits` | **assign**, `mode: auto` — shipwright fixes, pushes, re-triggers |
| `review:re-review` | **in flight** — the reviewer owes a verdict. Skip; waiting is correct. |
| `review:approved` | **clear** — Leader handles inline (below) |
| `ship:ready` | **done** — terminal. Never re-dispatch. |
| `ship:blocked` | already triaged and waiting on the user — don't re-dispatch or re-notify |
| `review:new` / unlabeled | **skip** — the reviewer hasn't ruled. That is `/pr-watch --backlog`'s job, not ours. |
| draft / `no-pr-watch` | **skip** |

**This is what makes running the reviewer on a different model safe.** Labels are written
by `pr_label.py`, not by model prose, so a vendor swap on the `/pr-watch` side changes
nothing here. Never key a decision on phrases like "LGTM" — see GOTCHAS.

## Where to look — the three lanes

One flat list of open PRs is unreadable at this scale (66 open, 58 non-draft on
2026-08-04). Split it by **whose court the PR is in**. These three partition the queue
exactly — every non-draft open PR is in one and only one:

```
# shared prefix
is:pr author:@me state:open archived:false -draft:true sort:updated-desc
```

| Lane | Add to the prefix | Who acts |
|---|---|---|
| **1 · Waiting on a reviewer** | `-label:review:blocker -label:review:nits -label:review:approved -label:ship:ready -label:ship:blocked -label:ship:escalated` | `/pr-watch`. Catches unlabeled PRs *and* `review:new` / `review:re-review` / `review:inconclusive` in one negation, so it needs no OR. |
| **2 · Waiting on an agent** | `label:review:blocker,review:nits,review:approved -label:ship:ready -label:ship:blocked -label:ship:escalated` | `/ship-watch`. A verdict exists; the fixing hasn't happened. |
| **3 · Waiting on you** | `label:ship:ready,ship:blocked,ship:escalated` | **You.** `ship:ready` = merge it, `ship:blocked` = decide it, `ship:escalated` = unstick it. |

Lane 2 excludes the court labels deliberately: a gated PR carries `review:blocker`
*and* `ship:blocked` at once, and it belongs in lane 3, not lane 2 — it is waiting on a
human, not on an agent. (PRs carrying a hard terminal disposition — `ship:parked`,
`ship:deferred`, `ship:superseded`, `ship:probe` — sit outside all three lanes: nothing
is waiting on anyone, and the scanner's `terminal` bucket never re-dispatches them.)

**Verify the partition after any taxonomy change** — lane counts must sum to the prefix's
own total, or a PR is double-counted or invisible:
```bash
gh api -X GET search/issues -f q='<lane query>' --jq '.total_count'
```

Two filters worth knowing about in a hand-written version of the prefix:
- **`no:assignee` is a trap.** It hides nothing today (no PR carries an assignee), but the
  first PR you assign to yourself silently vanishes from the view. Leave it out.
- **`archived:false` hides real work.** It drops PRs in archived repos — 2 today
  (`pbswi-youtube-analytics#10`, `wpm-casi-self-study#1`). They cannot merge until the
  repo is unarchived, so excluding them is defensible, but it is a decision, not a no-op.

## Single pass (default)

Each pass is a fresh context — the Team persists between invocations; your in-head state
does not. Read `.ship-watch-state.json` first, every pass.

### 1. Scan — one API call

```bash
python3 scripts/ship_scan.py scan --actionable-only
```

That wraps a single `gh search prs --state=open --author=@me`, which covers every repo
and org at once. **Do not fan out to per-repo `gh pr list`, and never read a diff in the
Leader.** Leader passes that stay cheap are the difference between this skill costing
pennies and costing hundreds of dollars (see GOTCHAS → cost).

Output is grouped per repo, ordered by how much actionable work each carries, with
`agent` (the teammate name), `local_path`, `needs_clone`, and per-PR `mode`.

On the **first pass**, if `.ship-watch-state.json` has no `started_at`, record one
(`date -u +%Y-%m-%dT%H:%M:%SZ`) and write the resolved `label_script`.

### 2. Clear the approved PRs yourself — do not spawn an agent for these

A `review:approved` PR needs no code change: just confirm the approval is at the current
head and CI is green, then stamp the clearance. Spawning a shipwright to do that would
burn an agent slot to write a label.

For each `clear` PR:
```bash
gh pr view <n> --repo <repo> --json headRefOid,statusCheckRollup,comments
```
- **Confirm the approval is at head.** The newest `<!-- pr-watch: sha=... -->` marker in
  the comments must equal `headRefOid`. If it doesn't, the approval is stale — commits
  landed after the review. Leave it alone; `/pr-watch` will re-review and move the label.
- **Confirm CI is green.** Every `statusCheckRollup` entry must be `SUCCESS`, `NEUTRAL`
  or `SKIPPED`. An empty rollup means the repo has no CI — treat as green, and say so in
  your summary rather than implying checks passed.
- Then:
  ```bash
  python3 <label_script> ensure <repo>
  gh pr edit <n> --repo <repo> --add-label "ship:ready"
  ```
  and post a one-line closing comment.

Count each toward `--budget`. Add the PR to `shipped[]`.

### 3. Ensure the Team exists (once per session)
```
TeamCreate({ team_name: "ship-watch", description: "Ship Watch — leader + per-repo shipwrights" })
```
If it already exists, skip.

### 4. Assign repos

For each repo with `assign` PRs, in the order `ship_scan.py` returned (most actionable
first):

1. **If the repo is already in the roster** and its teammate is alive, do not spawn a
   second. If it gained PRs since the last pass, `SendMessage` the additions. If the
   roster entry exists but the teammate is gone, respawn it and re-send the full list.
2. **If `needs_clone`**, include `clone_url` and let the shipwright clone into
   `scratch_dir` — **not** into `~/Developer`. Adding a repo to the workspace tree is the
   user's decision, not a side effect of a review loop.
3. **Write the roster entry before spawning**, so a crash mid-spawn doesn't orphan the PR.
4. Spawn:
   ```
   Agent({ name: "<agent from scan>", model: "sonnet",
           prompt: <contents of prompts/shipwright.md> + "\n\n## This dispatch\n<payload>" })
   ```
   Payload: `repo`, `prs` (each `{number, verdict, mode}`), `local_path`, `clone_url`,
   `scratch_dir`, `label_script`.

   Order `prs` with `mode: auto` first and `gated` last, so unattended work lands while
   the gated ones wait on you.

### 5. Verify progress on GitHub — never trust a teammate's word

This is the load-bearing step. Teammates in this workspace have repeatedly gone idle
without reporting; a silent agent is indistinguishable from a working one unless you
check the PR.

For each in-flight PR, compare the scan's `updated_at` and label against `progress[]`:

- **Moved** (label changed, or `updated_at` advanced) → reset `stalled_ticks` to 0.
- **No movement** → increment `stalled_ticks`.
  - `stalled_ticks == 3` → `SendMessage` the teammate asking for a one-line status.
  - `stalled_ticks == 5` → `TaskStop` it and respawn **once**, re-sending the full
    payload. Record `respawned: true`.
  - Already respawned and still stalled → stop chasing. Surface it to the user and drop
    the PR from `assigned[]`. Do not loop on it.

### 6. Retire

Retire a shipwright whose repo has no `assign` PRs left. `TaskStop` it to free the slot —
an idle teammate still holds its pane, so `idle` is not `freed`. Remove it from the
roster.

### 7. Respect the cap — pace and queue, never burst

Keep at most **6 live shipwrights**. This is a hard ceiling, not a preference: past ~8–9
concurrent agents, spawning fails outright with `fork failed: Device not configured`, and
6 leaves headroom for the Leader itself.

Dispatch at most `6 − live` new shipwrights per pass. Enqueue the overflow in `queue[]`
(the scan's order is already the right priority) and `log()` what you deferred — never
drop silently. Retiring in step 6 frees slots that pull from `queue[]` next pass.

### 8. Gated PRs — label them, batch the notification, don't nag

A gated PR is **waiting on the user**, and that has to be visible without reading this
session's transcript. When a shipwright reports it has triaged a gated PR and posted its
plan:

```bash
python3 <label_script> ensure <repo>
gh pr edit <n> --repo <repo> --add-label "ship:blocked"
```

Remove it the moment the PR stops waiting on the user — on release
(`--remove-label "ship:blocked"` before you `SendMessage` the shipwright to proceed), and
on any pass where the PR is no longer in `gated[]`.

`ship:blocked` is deliberately **not** a `review:*` state: it says nothing about the
review verdict, so it coexists with `review:blocker` and never touches the exactly-one-
review-label invariant. Together with `ship:ready` and `ship:escalated` it makes
*everything waiting on the human* a single query:

```
is:pr is:open author:@me label:ship:ready,ship:blocked,ship:escalated
```

(`label:a,b` is OR in GitHub search.) `ship:ready` = merge it. `ship:blocked` = decide
it. `ship:escalated` = unstick it. Nothing else should ever be in the user's court.

**Record duties.** When the Leader stamps `ship:ready` inline on a cleared PR, it also
posts a `terminal` record (`state: ready`) via
`~/.claude/skills/pr-watch/scripts/pr_record.py`'s `serialize()` in the closing
comment. The gated flow needs no record — `ship:blocked` is non-terminal; the triage
plan the shipwright posted is the evidence.

Then collect every PR in `gated[]` and send **one** `PushNotification` covering all of
them — not one per PR, and only when the set has changed since the last pass. When the
user replies `proceed <repo>#<n>` (or approves in bulk), remove the label and
`SendMessage` the owning shipwright to release that PR.

### 9. End the pass

**Never block waiting on a shipwright.** Report what was assigned and end the pass; the
shipwrights keep working in their own contexts. Run the skill again when you want another
pass — a shipwright's round is minutes, so re-invoking sooner than that buys nothing.

The pass is done when `budget` is reached, or when `assign` and `queue[]` are both empty
— then `PushNotification` a summary and stop.

## Scoped one-shot (`--repo <owner/repo>`)

For one repo right now, no team:
```bash
python3 scripts/ship_scan.py scan --repo <owner/repo>
```
Handle its `clear` PRs inline (step 2), then work its `assign` PRs **inline as the
Leader**, following `prompts/shipwright.md` yourself. No `TeamCreate`, no `Agent()`. This
is the right mode for testing a change to this skill, and for a single-repo day.

## Preview (`--preview`)

```bash
python3 scripts/ship_scan.py scan --actionable-only
```
Print a table — `repo · agent · #num · verdict · mode · needs_clone` — plus the summary
counts, then stop. Spawn nothing, post nothing, write no labels.

## Status (`--status`)

Report without changing anything:
1. Read `.ship-watch-state.json` — show the roster, `assigned[]`, `gated[]`, `shipped[]`,
   `queue[]`, and each PR's `stalled_ticks`.
2. Re-run the scan and print the live label for each tracked PR beside the recorded one —
   drift between them is the signal that a teammate died quietly.
3. Print a compact table: `repo · agent · PRs · state · stalled`.

Do **not** message teammates to build this; the PR state on GitHub is the truth and it is
one call.

## Round state (survives invocations)

Persist to `<worktree-root>/.ship-watch-state.json`; git-exclude it with
`echo .ship-watch-state.json >> "$(git rev-parse --git-path info/exclude)"` — that path
resolves correctly even in a worktree, where `.git` is a file.

```json
{ "started_at": "2026-08-04T14:00:00Z",
  "label_script": "/Users/…/pr-watch/scripts/pr_label.py",
  "scratch_dir": "/…/scratchpad/ship-watch",
  "roster":   { "mriechers/opnsense-config": "sw-mriechers-opnsense-config" },
  "assigned": { "mriechers/opnsense-config": [78, 79, 80] },
  "progress": { "mriechers/opnsense-config#79":
                { "updated_at": "2026-08-04T14:02:00Z", "label": "review:blocker",
                  "stalled_ticks": 0, "respawned": false } },
  "gated":    ["mriechers/github-actions#5"],
  "queue":    ["mriechers/second-brain"],
  "shipped":  ["mriechers/research-ops#24"],
  "budget":   10,
  "quiet_ticks": 0 }
```

## Guardrails

- **Never merge.** `ship:ready` + a closing comment + a notification is the terminal
  state. Closing a superseded PR or deleting a remote branch stays with the user.
- **Never work in a primary checkout.** Shipwrights use disposable worktrees under
  `scratch_dir`; the repo's own checkout stays on `main`, clean
  (`conventions/SESSION_SEPARATION.md`).
- **Never clone into `~/Developer`.** Missing repos are cloned into `scratch_dir`.
- **Blockers are gated by default.** Only `--auto-blockers` changes that, and only for
  the session it is passed in.
- **Exactly one `review:*` label per PR.** `pr_label.py` owns that invariant — never add
  or remove a `review:*` label with a bare `gh pr edit`. (`ship:ready` is not a
  `review:*` state and *is* applied with `gh pr edit`, after `pr_label.py ensure`.)
- **Only self-authored PRs.** The scan is `--author @me`; do not widen it. This drives
  your work to merge, it does not rewrite other people's PRs.
- **One leader per session.** `/ship-watch` and `/pr-watch` both spawn teams and should
  not share a session. Run the reviewer in its own — e.g. `claude-worktree` — which is
  also what lets you run it on a different model.

## When to use / not

- **Use:** you have PRs carrying reviewer verdicts across several repos and want the nits
  cleared without babysitting each round.
- **Not for:** producing the reviews (that's `/pr-watch`), driving a single PR you're
  actively working (that's `/ship-pr` directly), or merging (yours).

## Running under another agent (Gemini, Codex, …)

The **engine is harness-neutral**: `scripts/ship_scan.py` (stdlib + `gh`),
`prompts/shipwright.md`, `pr_label.py`, and the label protocol run under any agent with a
shell. Only the *orchestration* uses Claude Code primitives — `TeamCreate` / `Agent()` /
`SendMessage` / `TaskStop` (the persistent per-repo shipwrights). Without those, run the
inline equivalent: scan, then work each repo's PRs yourself following
`prompts/shipwright.md`, re-running when you want another pass. Per
`non-claude-agents.md`, do it from your own git worktree.

## Typical use

```
/ship-watch --preview            # see the queue first
/ship-watch --budget 5           # one pass, stop after 5 reach ship:ready
/ship-watch --repo mriechers/the-lodge   # one repo
```
