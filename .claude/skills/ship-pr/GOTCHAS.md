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

## A review can describe a completely different diff — verify it's about YOUR PR before actioning it (2026-08-11)

**What went wrong:** Driving `mriechers/the-lodge`#605 (12 files, +2654/−0), the `github-actions` reviewer posted a detailed, confident review of a **bootstrap/vendoring import** — "1,296 files, ~251k insertions, zero modifications or deletions", citing `git show 2cad16b --name-status` — and raised a residual concern about Alfred clipboard databases and per-machine local state entering version control. None of that content is in the PR. `git merge-base --is-ancestor 2cad16b HEAD` returns false: the commit it analyzed isn't even an ancestor of the branch.

**Why it matters:** the review was posted **at the correct head SHA**, so every freshness check in step 3 passes — the marker matches, the timestamp is current, the label is real. Staleness detection cannot catch this, because the review isn't stale; it's about the wrong thing. Step 4's triage assumes findings are *about your diff* and asks only whether each is technically sound. "Unaudited Alfred clipboard blobs" reads as a plausible, even serious, finding if you don't first ask whether the PR touches Alfred at all. The natural failure is to go remediate a secrets exposure that doesn't exist in your branch.

**Don't:** start triaging findings before confirming the review describes your PR. **Do:** before step 4, sanity-check the review's own claims against the PR's actual shape — `gh pr view <n> --json additions,deletions,changedFiles`, and if the review cites a commit, `git merge-base --is-ancestor <sha> HEAD`. A file count off by two orders of magnitude, or a cited commit that isn't an ancestor, means the reviewer computed the wrong diff range (a shallow checkout with no real merge-base produces exactly this "100% additions" signature). Reject it explicitly in your reply with the numbers — don't silently ignore it, or the next round re-raises it — and file the reviewer bug separately (see mriechers/github-actions#18). Pushing back here is not the contested-nit case; it costs no `pushback_only_rounds`, because the finding was never about your code.
