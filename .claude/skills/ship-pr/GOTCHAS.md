# Gotchas — /ship-pr

## Repo review Actions may post NOTHING when clean (second-brain)

**What went wrong:** On `mriechers/second-brain`, the claude-review Action is triggered by the `claude-review` label (not `@claude` comments, not pushes), runs with `use_sticky_comment: false` + `classify_inline_comments: true`, and posts **zero output anywhere** when it has no findings. All three review surfaces (comments, formal reviews, inline threads) came back empty and this initially read as "review never ran."

**Why:** The skill's step 2/3 assumes a verdict artifact always exists. Some Actions only emit *findings* — a clean pass is the absence of inline comments, not an LGTM comment.

**Don't:** treat empty review surfaces as "no review yet" without checking the run itself. **Do:** verify via `gh run view <id> --log | grep -E '"num_turns"|Trigger result|No buffered inline comments'` — `Trigger result: true` + several turns + "No buffered inline comments" on the current head **is** the clean-pass verdict. To re-trigger after a push on label-triggered repos, remove and re-add the label (`gh pr edit <n> --remove-label claude-review` then `--add-label`); a `@claude please review` comment alone may not fire it.

## Review Action edits ONE comment in place — polling `.comments[-1]` sees false verdicts (pbswi)

**What went wrong:** On `public-media-work/pbswi`, the claude-review Action posts a single status comment and **edits it in place** through its lifecycle: `"Claude Code is working… I'll analyze this"` → a `Working…` checklist with unchecked boxes → the final verdict. Polling `.comments[-1]` for "an author=claude comment newer than my trigger" fired **twice on non-verdicts** — first on the "working" placeholder, then on the checklist — before the real verdict landed. Each false hit reads as "re-review done" and nearly triggered a premature Done.

**Why:** Step 7 ("wait for the re-review") assumes the verdict arrives as a *new* comment with a later timestamp. When the Action mutates one comment in place, `createdAt` stays fixed at the placeholder's time and the *body* is what changes — so timestamp/author checks are blind to it.

**Don't:** gate "re-review complete" on a newer claude comment alone. **Do:** poll on a settled signal — either the `claude-review` **check leaving `pending`** (`gh pr checks <n>` → `pass`/`fail`), or the latest comment **body no longer containing** `Working…`/`I'll analyze`. Both are reliable where the bare "newest comment" heuristic is not.

## Bare `git push` rejected on this repo — use an explicit refspec

**What went wrong:** `git push` (no args) was rejected `! [rejected] ... (fetch first)` / "tip is behind" even though `git ls-remote` proved the branch was a clean fast-forward from the remote tip. `git push origin HEAD:refs/heads/<branch>` succeeded immediately.

**Why:** the parent repo has submodules; a bare push tries to validate/push more than the current branch (submodule-aware push config + a stale remote-tracking ref that a submodule-recursing `fetch` didn't refresh), and one of those made push refuse. The explicit single-branch refspec sidesteps it.

**Don't:** `git pull`/rebase in a panic when a bare push is rejected but `ls-remote` shows a fast-forward. **Do:** push the exact refspec — `git push origin HEAD:refs/heads/<branch>` — and re-verify with `git ls-remote origin refs/heads/<branch>`.

## Mention-triggered re-review is a workflow_run, NOT a new commit check-run (pbswi)

**What went wrong:** After the in-place-comment gotcha above bit me (my first watcher fired on the "Review in progress" checklist), I pivoted to polling `gh api repos/<r>/commits/<sha>/check-runs` for a *new* `claude-review` run to reach `completed`. It timed out at ~20 min even though the review had actually finished in 1m35s. The `@claude please review` mention triggered **workflow run 29430152313**, which edited the status comment to the final verdict — but the commit's check-runs list kept only the **original** `claude-review` run (from PR open). `gh pr checks` likewise still showed the old run. So "watch for a new check-run / check leaving pending" never fires for the mention-triggered path.

**Why:** the auto-on-open review registers as a commit check-run; the `@claude`-comment-triggered re-review is a separate `workflow_run` that reports via the PR comment, not a new commit check. Watching commit check-runs conflates the two.

**Don't:** poll `commits/<sha>/check-runs` for the mention-triggered re-review — it can sit unchanged while the review completes. **Do:** grab the run id from the "in progress" comment's `[View job run](…/actions/runs/<ID>)` link and poll `gh run view <ID> --json status,conclusion` until `completed`, and/or watch the status comment **body** settle to a verdict (no `in progress`/`Working…`/unchecked checklist). The body-settling signal from the entry above is the most portable.

## Poll loops: `[ "$a" \> "$b" ]` string comparison errors under zsh

**What went wrong:** Waiting for a re-review (step 7), I backgrounded a poll loop comparing ISO timestamps with `[ "$latest" \> "$ref" ]`. Under zsh (this machine's default shell, which the Bash tool inherits) that throws `condition expected: >` on **every** iteration — the loop ran to its full count and reported a false `TIMEOUT`. The verdict had actually landed; I only caught it by reading the comment directly afterward.

**Why:** `\>`/`\<` string comparison inside single-bracket `[ ]` is a bash-ism; zsh's `[ ]` rejects it. Easy to miss because the loop doesn't crash — it just never matches.

**Don't:** use `[ "$a" \> "$b" ]` for string/timestamp comparison in a poll loop here. **Do:** prefer the settled-signal polls this file already recommends — `gh run view <ID> --json status --jq '.status'` == `completed`, or body-settling — which avoid timestamp math entirely. If you must compare strings, use `[[ "$a" > "$b" ]]` (double brackets) or `jq`.

## The `/pr-watch` bot baseline-gates to post-startup PRs — a PR opened mid-session may never get reviewed (2026-07-21)
**What went wrong:** Ran `/ship-pr` on the-lodge#442, opened mid-session. The GitHub `claude-review` Action was out of Actions minutes (3s failure), so the review surface was the local `/pr-watch` bot's comment. The bot never posted — after ~24 min / 3 poll ticks, all three surfaces (issue comments, inline threads, formal reviews) were empty. Cause: `/pr-watch` baseline-gates — it auto-adopts only PRs opened *after the bot itself started*, shelving earlier ones as backlog. #442 was opened while the bot was already running, so it was never adopted.
**Why it happened:** ship-pr assumes a review will eventually land and polls for it. But if the reviewer (pr-watch) never adopted the PR, the poll runs forever against a review that won't come.
**Don't:** Poll indefinitely for a review. **Do:** After ~2 empty ticks, check whether the reviewer even adopted the PR (pr-watch baseline-gates to post-startup PRs; the capped `claude-review` Action produces a silent/failed check, not a verdict). Surface to the user that the bot hasn't picked it up — it may need a nudge, or the PR predates the bot's start — instead of churning. The guardrail's pushback-counter doesn't catch this (no findings to push back on); an empty-review counter does.

## The visible reviewer login is `claude[bot]` on pbswi (not `github-actions[bot]`)

**What went wrong:** 2026-07-22, driving pbswi #193/#180/#197 to merge-ready. My re-review poll filtered comments by `user.login == "github-actions[bot]"` (per `/start`'s guidance) and timed out for 9 min on #193 — the `@claude` verdict actually posts as **`claude[bot]`** on this repo.

**Why:** the interactive reviewer's GitHub-App identity is repo-specific; pbswi's is `claude[bot]`. The silent auto-check is still `review / claude-review` (posts nothing).

**Don't:** filter the re-review poll by a hard-coded bot login. **Do:** as the entries above already say — poll the `[View job run](…/actions/runs/<ID>)` run id to `completed`, or watch the status-comment **body** settle to "Claude finished"/a verdict (not the in-progress checklist). If you must match by author, accept `claude[bot]` OR `github-actions[bot]`.

## An already-adopted PR that gets force-pushed can still go un-re-reviewed — don't block on it (2026-07-23)
**What went wrong:** During a `/ship-pr` run with 4 concurrent PRs, `/pr-watch` re-reviewed #460 and #461 on their new heads but never re-reviewed #454 after a triage-rebase force-push — the re-review watcher timed out at 25 min. #454 was already adopted (it had an earlier conditional approval), so this wasn't the post-startup baseline-gating case; it read as per-PR teammate starvation under the concurrent-agent cap (and/or a force-push not registering as a new-commit trigger). Filed the-lodge#463.
**Why:** ship-pr step 7 waits for the re-review to land on the current head, but pr-watch is commit-triggered (not `@claude`-triggered), and with many PRs open its per-PR teammate can be starved — the verdict simply never comes for that one.
**Don't:** block a merge indefinitely on a re-review that may never arrive. **Do:** when the earlier review already approved (or approved-pending-a-now-met condition) AND the new head is a mechanical, self-verified change (e.g. a conflict-free rebase you re-ran the tests on), prior approval + local verification can carry the merge — with the user's explicit go. Pair with the "baseline-gates to post-startup PRs" entry above: after ~2 empty ticks, stop polling and decide, don't churn.

## No GitHub review Action + pr-watch posts under the human account — `@claude` re-trigger is a no-op (skill-vetting, 2026-07-23)

**What went wrong:** Drove `mriechers/skill-vetting`#5. The initial pr-watch review was posted under login **`mriechers`** (the user's own gh identity — pr-watch runs locally and comments as the authenticated user, not a bot), and the repo has **no `claude-review` GitHub Action** (`gh pr checks` → "no checks reported"). After I pushed the fix and commented `@claude please review`, the ~6-min re-review poll timed out: nothing server-side listens for `@claude` here, and the local pr-watch loop wasn't running this session, so no re-review could land.

**Why:** Step 6 assumes an `@claude`-mention or push re-triggers a server-side reviewer. On a repo whose only reviewer is a local `/pr-watch` loop, the trigger is **a new commit landing while that loop is running** — the mention does nothing, and if the loop isn't up, no re-review comes at all. Combined with the line-43 baseline-gating entry: a review comment existing from PR-open does **not** imply a re-review will follow.

**Don't:** poll indefinitely for a re-review on a repo with no review Action, and don't assume the reviewer is a `*[bot]` login. **Do:** early on, check `gh pr checks` — "no checks reported" means there's no Action, so the only reviewer is a (possibly-offline) local pr-watch. Match the initial reviewer's actual login (may be the human account). If the substantive review already cleared it and the only open findings are ones you've fixed, surface that to the user and let them decide to merge, rather than churning on a re-review that can't fire.

## The `pr-watch: sha=` marker is a freshness test, not a wait-for-review signal (2026-08-10)

**What went wrong:** Driving the-lodge#610 through four rounds, my first re-review watcher polled for `pr-watch: sha=<head>` and timed out at ~13 min while a completed review sat on the PR. The reviewer that round was the **GitHub Action**, which never writes that marker — it is `/pr-watch`'s format. Two different reviewers, two different surfaces, and only one of them stamps a SHA.

**Why:** step 3 correctly uses the marker to decide whether a verdict is *stale*, so it reads like the canonical review signal. It isn't. It answers "was this verdict written against the current head?" and says nothing about "has a verdict arrived at all" — on a repo where the Action is the active reviewer, waiting for it waits forever.

**Don't:** key a wait loop on the `pr-watch: sha=` marker. **Do:** keep using it for the staleness test in step 3, and wait on the settled signals the entries above already name — `gh run view <ID> --json status` == `completed` (the ID is in the placeholder comment's `[View job run]` link), or the status-comment body settling to "Claude finished". One is a freshness question, the other is a completion question; the same string cannot answer both.

**Related:** the-lodge#594 (step 7 still describes a `createdAt`-based poll that this file has contradicted since 2026-07-22 — I wrote that exact broken watcher because I followed SKILL.md and never opened GOTCHAS.md).

## A reviewer that dies MID-FLIGHT is invisible — `review:re-review` looks identical to a healthy queue (skill-vetting #34, 2026-08-06)

**What went wrong:** Drove `mriechers/skill-vetting`#34. `/pr-watch` was demonstrably alive and engaged: it observed my push, pre-wrote `review:blocker` → `review:re-review` at 00:32:54Z, queued the PR, and updated its state file a minute later. Then the loop died. #34 sat at queue position 1 for ~3 hours with zero comments, still carrying `review:re-review`. My first monitor timed out after 45 min with no events at all — because every signal I was watching for was an *arrival*, and nothing was ever going to arrive.

**Why it happened:** This is NOT the "no Action + loop wasn't running" case in the 2026-07-23 entry above — that entry's detection advice (`gh pr checks` → "no checks reported") passes cleanly here, because the reviewer *was* running at dispatch time and did react. The failure is that pr-watch's liveness is unobservable from the PR surface: a `review:re-review` label written by a since-dead loop is byte-identical to one written by a live loop with a full roster. Step 7's "poll until a comment newer than your push appears" then polls forever. (Root cause filed as the-lodge#574 / #575 — the stale label and the phantom-full roster.)

**Don't:** treat silence as "still queued," and don't write a monitor whose only emissions are success signals — if the reviewer crashed, silence and in-progress are the same observation. **Do:** check liveness out-of-band, which the PR surface cannot tell you:
```bash
ps aux | grep "[c]laude" | grep "loop\|pr-watch"        # no loop process = dead
ls -l ~/Developer/the-lodge/.pr-watch-state.json        # mtime >> its ~30m tick = dead
python3 -c "import json;d=json.load(open(...));print(d['queue'],len(d['roster']))"
```
A roster at the 8-agent cap with zero live agents is self-deadlocking — a restart inherits it and dispatches nothing. Arm any wait with an explicit staleness guard alongside the happy path, and when the reviewer is confirmed dead, stop and put the decision to the user rather than churning.

## Actions billing can refuse the job — Done becomes unreachable and the loop churns (the-lodge, 2026-07-26)

**What went wrong:** Ran six fix rounds across the-lodge #466/#465/#464/#459/#468 (+ follow-up #477), pushed each, posted `@claude please review` each time. Zero reviews landed. The mention *did* fire the workflow every time — but every run was `completed/failure` in 2–3s with the annotation *"The job was not started because recent account payments have failed or your spending limit needs to be increased."* The runner is refused before the reviewer executes, so step 7 waits for a re-review that can never arrive.

**Why:** Step 6 treats "workflow enabled + mention posted" as sufficient for a re-review. It isn't — the job still has to *start*. A billing/spending-limit block produces a red check that looks like a review failure, and a `disabled_manually` workflow produces no check at all; both read as "the reviewer is being slow" rather than "the reviewer cannot run." Enabling the workflow (which I did, expecting it to fix the loop) changed nothing but the shape of the failure.

**Don't:** enter the loop, or re-enable a paused review workflow, without confirming the job can actually start. **Do:** at step 1, run `gh run list --repo <r> --workflow claude.yml --limit 3 --json conclusion,createdAt` — a run pattern of 2–3s failures means check the annotation (`gh run view <id>` → ANNOTATIONS) before doing any work. If it's a billing/quota refusal, say so, do the fix-and-push half, and stop: the sign-off half is unavailable on **every** repo, so there's no point re-triggering. See the-lodge #440 for the standing record.

## A PR can merge mid-loop — rebase the fixes rather than pushing to a deleted branch (the-lodge #470, 2026-07-26)

**What went wrong:** Was addressing the review on #470 when it got squash-merged by someone else. `git push HEAD:chore/claude-review-pause-tool` reported `* [new branch]` — it had *recreated* the deleted head branch — and `gh pr view 470` still showed the old head SHA, so the fixes appeared to have silently not landed.

**Why:** Step 1's pre-flight gate checks `state` once, at the top of the round. A long round (reading review, editing, running tests) leaves a wide window. On merge, GitHub deletes the head branch; a later push to that refspec succeeds by creating a fresh branch that no PR points at — no error, no warning.

**Don't:** trust the step-1 state read after a long round, and don't read `* [new branch]` on a push to an existing PR branch as benign — it means the branch was gone. **Do:** re-check `state` immediately before pushing. If it merged, rebase the work onto the *new* `origin/main` (`git checkout -b <fix-branch> origin/main && git cherry-pick <sha>`) and open a follow-up PR cross-referencing the merged one, then delete the branch your push recreated.

## A green `claude-review` check does NOT mean a review was posted (wonder-cabinet-episode, 2026-07-26)

**What went wrong:** Treated `claude-review SUCCESS` in `statusCheckRollup` as "the reviewer approved" across four PRs. It never had. The workflow granted `pull-requests: **read**`, so the Action ran the full review — 7m23s and 9m07s of billed time — reported **success**, and silently published nothing. I repeated "claude-review SUCCESS" to the user as evidence of a passing review before discovering the check was structurally incapable of producing one.

**Why it happened:** The check reports on whether the *job* exited cleanly, not on whether a review reached a review surface. A reviewer that cannot post still exits 0. The skill's step-2 instruction to read all three surfaces catches this — but only if you actually read them instead of trusting the rollup, and the rollup is right there in the step-1 output.

**Don't:** infer a verdict from a check name and conclusion. **Do:** a green `claude-review` with **0 issue comments + 0 reviews + 0 inline threads** means the reviewer *failed to speak*, not that it had nothing to say — treat it as a broken reviewer and go look at the run. Two tells in the run's result JSON: `permission_denials_count > 0`, and a suspiciously short `num_turns`/`duration_ms` (3 turns / 36s vs the 7–9 min a real review takes). Also check the `GITHUB_TOKEN Permissions` block in the run log — `PullRequests: read` cannot post.

## A PR that edits the review workflow cannot test its own change (2026-07-26)

**What went wrong:** Told the user the reviewer-permission fix could be observed working on the very PR that made it. It can't. `anthropics/claude-code-action` refuses to run when the workflow file differs from the default branch: *"Workflow validation failed. The workflow file must exist and have identical content to the version on the repository's default branch."* The run exits in ~15s, conclusion **success**, no review — indistinguishable at a glance from the bug being fixed.

**Why it happened:** By design — it stops a PR from granting itself elevated permissions and running modified reviewer code in the same breath. Sensible, and easy to miss because the skipped run still reports green.

**Don't:** promise verification on a workflow-editing PR, and don't read its short green `claude-review` as either success or failure. **Do:** say plainly that the change is unverifiable until merged, name the first real test (the next PR into the protected branch that does *not* touch that workflow file), and batch further workflow changes — each attempt costs a merge to `main` before it can be checked. When auditing prior runs as evidence, first confirm they didn't hit this guard (`grep -c "workflow validation"`), or the whole diagnosis rests on runs that never ran.

## The self-authored check passes for other *agents'* PRs on a shared account (2026-07-26)

**What went wrong:** Ran `/ship-pr 60` on wonder-cabinet-episode. The skill's guard — `gh pr view --json author` vs `gh api user` — matched (`mriechers` == `mriechers`) and cleared me to drive it. But #60 was authored by a *different autonomous agent* working in its own `.herdr` worktree, using the same gh identity. The login check cannot distinguish "my work" from "another agent's work" when several agents share one GitHub account.

**Why it happened:** The guard assumes login identity maps to authorship. In a multi-agent workspace it maps to *machine*, not to *session*. Nothing about the check is wrong; its premise just doesn't hold here.

**Don't:** treat a login match as proof the PR is yours to push to. **Do:** in a workspace with concurrent agents, add a liveness check before touching the branch — is there a worktree holding it (`git worktree list`), is it dirty, has anything been written recently (`find <wt> -newermt '10 minutes ago' -type f -not -path '*/.git/*'`)? Clean and idle is the safe window; active means hand it back rather than push under it. Force-pushing a branch a running agent owns strands its work and any branch stacked on it. (In this instance the pre-flight `state: MERGED` gate stopped the run first — but it would not have on an open PR.)

## Step 5 says "commit" — verify with `git status`, never `git diff` (skill-ops #76, 2026-08-14)

**What went wrong:** Preparing a one-file fix, I ran a combined "suite + all CI checks" command that hit the Bash tool's 2-minute timeout and was killed mid-run. That killed a test between the scaffold it creates in the repo and its trailing `rm -rf`, leaving nine files behind. I then ran `git diff --stat`, saw exactly the one file I meant to change, and committed with `git add -A`. The junk went to the PR. CI reported `invalid JSON` on a path that does not exist on the base branch — confusing, because the file is untracked locally and tracked only in the bad commit.

**Why:** `git diff` and `git diff --stat` **do not show untracked files**. Checking the diff before `git add -A` verifies nothing about the thing `-A` is most likely to sweep in. A killed command makes this far more likely, because interrupted tests skip their cleanup — and some suites write into the real working tree rather than a temp copy (skill-ops #77).

**Don't:** use `git diff` as the pre-commit check, and don't assume a timed-out command left nothing behind. **Do:** run `git status --porcelain` — untracked entries show as `??` and `git diff` omits them. Treat any killed or timed-out command as a specific trigger to re-check. Prefer naming paths explicitly over `git add -A` when a test run preceded the commit. If it already landed, `git rm -r --cached <path>` + `git commit --amend` + `git push --force-with-lease` is the repair.
