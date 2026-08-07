---
name: ship-pr
description: >
  Author-side loop that drives one of your own pull requests to a clean, merge-ready
  state: read the latest review feedback, address what's sound (push back on what
  isn't), push fixes, let the review re-run, and repeat until the reviewer signs off
  AND CI is green. The counterpart to /pr-watch (which posts the reviews). Built to
  run under /loop so it self-paces on each new review. Stops and notifies when
  merge-ready — it never merges for you. Triggers on "ship pr", "ship this pr",
  "drive my PR to merge", "address the review", "iterate on PR <n> until it's ready".
---

# Ship PR

The **author** side of the PR loop. `/pr-watch` (and, on most repos, a per-commit
GitHub Action that runs a single review agent) *posts* feedback on your PRs. This
skill *consumes* that feedback: triage it, fix what's right, resubmit, and loop until
the reviewer agrees the PR is mergeable and CI is green — then **stop and hand the
merge to you**.

**Arguments:** `$ARGUMENTS`

| Arg | Meaning |
|---|---|
| (none) | Infer the PR from the current branch (`gh pr view --json …`). |
| `<number>` or a PR URL | Target that PR (the repo is inferred from the current checkout). |
| `--repo <owner/repo> --pr <n>` | Target a PR in another repo explicitly. It gives you no local branch to push to — use it to *inspect*; to *drive* a PR (push fixes), `cd` into that repo's checkout first. |

Designed for `/loop ship-pr <n>` (dynamic mode): each tick is one review cycle. It
self-paces on the review re-running (~2–3 min), not a fixed interval.

## Self-authored only

Act only on PRs **you** authored. This drives *your* work to merge; it does not
review or rewrite other people's PRs. **Verify before doing anything else** and abort
if it isn't yours:
```bash
test "$(gh pr view <n> --repo <r> --json author --jq '.author.login')" \
   = "$(gh api user --jq '.login')" || echo "not self-authored — stop"
```

## The loop (one cycle per /loop tick)

### 1. Resolve the PR and its state
```
gh pr view <n> --repo <r> --json number,state,isDraft,headRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,headRefOid
gh pr checks <n> --repo <r>
```
**Pre-flight gate — bail before doing any work if the PR isn't shippable:**
- `state` is `MERGED` or `CLOSED` → **already shipped (or abandoned); stop.** Nothing
  to do — don't read reviews, don't loop, don't notify. (Common when the loop is
  re-invoked on a PR you already merged — exit cleanly instead of churning.)
- `isDraft` is `true` → it's a draft; the review Action usually won't run and it
  isn't meant to merge yet. Tell the user and stop — or, only if they confirm, mark it
  ready (`gh pr ready <n>`) first, then proceed.
- `mergeable` is `CONFLICTING` (or `mergeStateStatus` is `DIRTY`) → **merge conflicts
  with the base.** Surface this and stop; resolving them is a rebase that may need the
  user's judgment — don't paper over it. Once they say go, rebase onto the base
  branch, push, and resume the loop.
- `mergeable` is `UNKNOWN` → GitHub is still computing mergeability (common right
  after a push). Don't proceed on it — wait one tick and re-check. The `/loop`
  heartbeat handles this naturally; it's called out so the fall-through isn't mistaken
  for an oversight.

Otherwise (`OPEN`, not draft, not conflicting): confirm you're on (or check out) the
PR's head branch before editing — run all work from that branch / its worktree.
**Note the current head SHA (`headRefOid`)** — you'll use it in step 3 to ignore
feedback from earlier commits.

### 2. Read the latest review — all three surfaces
A review can land in three places. Read **all** of them, or you'll silently miss
findings:
```bash
# top-level issue comments (the per-commit Action posts its verdict here)
gh pr view <n> --repo <r> --json comments --jq '.comments[-1] | "\(.author.login) \(.createdAt)\n\(.body)"'
# formal reviews (humans; carries an approve/request-changes state + commit SHA)
gh pr view <n> --repo <r> --json reviews --jq '.reviews[] | "\(.author.login) [\(.state)] @\(.commit.oid[0:7]): \(.body)"'
# inline line-level review threads (commonly where real findings live — easy to miss).
# NOTE: `gh pr view` has NO reviewThreads field — you must use the GraphQL API.
# (first:100 covers any realistic PR; `line` is null for file-level comments, handled below.)
gh api graphql -F owner=<owner> -F repo=<repo> -F pr=<n> -f query='
  query($owner:String!,$repo:String!,$pr:Int!){ repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){ reviewThreads(first:100){ nodes{ isResolved
      comments(last:1){ nodes{ author{login} path line body } } } } } } }' \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved|not) | .comments.nodes[-1] | "\(.author.login) \(.path)\(if .line then ":\(.line)" else "" end): \(.body)"'
```
Read the **newest** feedback for the **current head commit** — older rounds are
already handled.

### 3. Detect the verdict

**Check for a `review:*` label first.** `/pr-watch` writes exactly one with every
verdict (`pr_label.py`), so when present it *is* the verdict and you can skip the
comment scraping:

| Label | Meaning here |
|---|---|
| `ship:ready` / `review:approved` | **Merge-ready** → **Done** |
| `review:nits` | **Findings present** (minor only) → step 4 |
| `review:blocker` | **Findings present** (must-fix) → step 4 |
| `review:new` / `review:re-review` | **No verdict yet** — wait for the reviewer |

The label is still subject to a staleness check, but use the exact signal rather
than a timestamp: the findings comment posted alongside it ends with
`<!-- pr-watch: sha=<head_sha> -->`, naming the SHA the reviewer actually read.
Discard the label unless that marker equals the current head SHA from step 1.
Equality beats a clock here — an amend or rebase moves the head without advancing
the comment's timestamp, so a timestamp test can call a stale verdict fresh.

**No label?** Look for a bare `pr-watch: sha=` marker next — a pre-#498 review, or one
whose label was removed, is still a real verdict and classifies by its severity.
Note the marker is the *only* reliable signal: `/pr-watch` posts through your own
token, so its verdicts are authored by you, not a bot. Never detect a review by author.

Still nothing? Fall back to reading the comments. First, **discard anything stale**:
ignore reviews/comments whose commit SHA (or timestamp) predates the current head
SHA from step 1. A pre-push "LGTM" must NOT trigger the Done path. Then classify
what remains:
- **Merge-ready** — the reviewer signs off *on the current commit* in substance:
  "ready to merge", "nothing new to flag", "LGTM", an approving review at the head
  SHA, no open actionable findings. → go to **Done**.
- **Findings present** — the review lists actionable items (severity-tagged like
  Medium/Low/Nit, or prose asks). → go to step 4.
- **Ambiguous / no new review yet** — if you pushed and the review hasn't re-run,
  wait (the /loop heartbeat brings you back). If the review is unclear, ask the user
  rather than guess.

### 4. Triage with rigor — do NOT blindly implement
Apply real technical judgment to every finding (the `superpowers:receiving-code-review`
skill captures this discipline if your harness has it; the rule is the same either
way):
- **Sound** (real bug, correctness, missing test, genuine clarity win) → fix it.
- **Wrong or contested** (reviewer misread the code, a false positive based on a
  partial CI checkout, a subjective call you disagree with) → do **not** implement it.
  Reply on the PR with a concise, technical rebuttal and leave it. Performative
  agreement that degrades the code is a failure, not a pass.
Track which findings you fixed vs. pushed back on — you'll summarize both, and the
push-back count feeds the guardrail (see below).

### 5. Apply, verify, commit, push
- **Clear a stale clearance first:** if the PR carries the `ship:ready` label from a
  prior pass, remove it at the **start** of this round —
  `gh pr edit <n> --repo <r> --remove-label "ship:ready"`. Do this before the head
  moves, not after the push, so there's no window where `ship:ready` sits on a
  soon-to-be-superseded head. It's re-applied only when re-review re-confirms at **Done**.
- Make the fixes. **Run the repo's tests and linters** (the goal is mergeable +
  green, not just "review-silent"). Don't mark anything done on a red bar.
- Commit with the repo's convention (conventional-commit subject + the
  `Agent:` / `Machine:` / `Co-Authored-By:` trailers — see this repo's CLAUDE.md,
  commit-conventions section). One commit per review round reads cleanly in history.
- `git push`.

### 6. Re-trigger and reply
- **The review Action is comment-triggered, not push-triggered.** On these repos a
  fresh push alone does *not* re-run the reviewer — you must post `@claude please
  review`. (A queued `claude-review` check that sits `pending` for minutes after a
  push is the tell that it's waiting for the mention.) Always post the mention.
- Fold it into a short reply that lists what you fixed (with the commit SHA) and what
  you pushed back on and why, ending with `@claude please review`. That one comment
  both re-triggers the review and leaves the paper trail the next round reads.

### 7. Wait for the re-review
The review job takes ~2–3 min. Under `/loop`, end the turn (schedule the heartbeat);
the next tick re-reads the verdict. Outside `/loop`, poll `gh pr view … comments`
until a comment newer than your push appears.

## Done (merge-ready)

When the reviewer signs off **and** `statusCheckRollup` is green **and** no actionable
finding is unresolved:
1. **Apply the `ship:ready` label** — bootstrap the taxonomy first, then a plain edit:
   ```bash
   python3 ~/.claude/skills/pr-watch/scripts/pr_label.py ensure <r>
   gh pr edit <n> --repo <r> --add-label "ship:ready"
   ```
   Never `gh label create` it by hand — the label's color and description have exactly
   one definition (`pr_label.py` `TAXONOMY`), and `ensure` writes it. (`pr_label.py set
   ship:ready` exits 2 by design: shipping disposition is not a review verdict, so
   `ship:*` labels go through `gh pr edit` after `ensure`.)
   This is the queryable signal that the loop cleared the PR: it feeds the human
   court queue (`gh api -X GET search/issues -f q='is:pr is:open author:@me
   label:ship:ready,ship:blocked,ship:escalated'` — `label:a,b` is OR; `gh search prs
   --label` cannot express OR and silently returns 0). It's the terminal-until-revoked
   member of the `ship:*` disposition axis (sits downstream of `claude-fix` → this
   loop). The label is applied **only here** and removed on any new commit (step 5), so
   its presence always means "loop-cleared on the current head."
2. Post a closing comment: quote the reviewer's sign-off, summarize the PR, state
   "ready to merge — leaving the merge to you."
3. Send a one-line **PushNotification** (the user may be away).
4. **STOP the loop. Do NOT merge.** Merging to main is a human-owned, hard-to-reverse
   action — surface that it's ready and let the user pull the trigger. If they've
   *explicitly* pre-authorized auto-merge for this run, that's the only exception.

## Protocol records

Your round comments carry **pr-review:v1 records** (compact JSON in HTML-comment
markers; generate with `~/.claude/skills/pr-watch/scripts/pr_record.py`'s
`serialize()` — never hand-write the JSON):

- **After triage** (step 4): one `disposition` record per finding you acted on —
  `status` `fixed` (with the commit SHA as `evidence`), `contested` (with the
  rebuttal), `deferred` (with the follow-up reference), or `redesign`. Use the
  reviewer's stable finding ids; that is how the open set shrinks by set difference
  instead of prose re-derivation.
- **At an exit**: a `terminal` record — `state` `ready` at Done, `escalated` on a
  hard stop / contested-round limit / no-progress timeout, `parked` for redesign,
  `deferred`, `superseded`, or `probe`.

Round counters are rebuildable from the PR thread via `pr_record.rebuild_counters()`
— the state file below is a cache, never the source of truth. The full protocol lives
in this repo's `planning/` unified spec.

## Round state (survives across /loop ticks)

Each `/loop` tick is a **fresh context** — counters held only in your head are lost
between ticks. Persist the loop's state to `<worktree-root>/.ship-pr-state.json` and
read it at the start of every tick. To exclude it locally use
`echo .ship-pr-state.json >> "$(git rev-parse --git-path info/exclude)"` — that path
resolves correctly even in a **worktree**, where `.git` is a file and
`.git/info/exclude` does not exist (writing to the literal `.git/info/exclude` fails there):
```json
{ "pr": 323, "repo": "mriechers/the-lodge",
  "last_head_sha": "<sha you last reviewed>",
  "pushback_only_rounds": 0,
  "rounds": 3 }
```
- On each tick: load it; if the current head SHA matches `last_head_sha` and no new
  review has landed, just wait (no work to do).
- After a round where you pushed back on *every* finding and fixed none, increment
  `pushback_only_rounds`; reset it to 0 on any round where you fixed something.
- This is what makes the contested-nit guardrail below actually enforceable across
  ticks.

## Guardrails / stop conditions

- **Never merge.** Stop-and-notify is the contract.
- **Don't loop forever on contested nits.** When `pushback_only_rounds` reaches 2
  (two consecutive rounds surfacing only findings you're pushing back on, no new sound
  issues), stop and ask the user how to resolve the disagreement instead of churning.
- **Red CI that isn't review-related** (flaky infra, unrelated failure) → surface it
  to the user; don't paper over it to force a green.
- **Scope discipline.** Address the review; don't sprawl into unrequested refactors.
- **One PR at a time.** This skill drives a single PR; for sweeping many, pair with
  `/backlog` or `/burn-down` to pick the PR, then `/ship-pr` it.

## When to use / when not

- **Use:** you have an open PR collecting review feedback and want it driven to
  merge-ready without babysitting each round in your active session.
- **Not for** opening the PR (just `gh pr create` / your commit flow), reviewing
  *others'* PRs (that's the Action / `/pr-watch`), or merging (yours to do).

## Typical invocation
```
/loop ship-pr 129
```
Runs a cycle now, then self-paces on each re-review until #129 is merge-ready, then
notifies you and stops.
