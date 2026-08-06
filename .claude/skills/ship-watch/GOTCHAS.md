# ship-watch — GOTCHAS

- **"Waiting on the human" must be one query, or the loop hasn't finished its job.**
  Shipping work nobody looks at is not shipping. Three labels carry it, and only these
  three: `ship:ready` (merge it), `ship:blocked` (decide it), and `ship:escalated`
  (unstick it). One saved search covers all of them —
  `is:pr is:open author:@me label:ship:ready,ship:blocked,ship:escalated` (`label:a,b`
  is OR). Nothing else belongs in the user's court, and every tick reports
  `in_your_court` so the queue can't quietly grow unread.

  **Build that query with `gh api`, not `gh search prs`.** The `--label` flag has no
  comma-OR, and repeating it means AND — so both of these return **0 without an error**,
  which reads as "nothing is waiting on me" and is the one failure mode this queue must
  never have:
  ```bash
  gh search prs --author=@me --label "ship:ready,ship:blocked"        # 0 — wrong
  gh search prs --author=@me --label ship:ready --label ship:blocked  # 0 — AND, wrong
  gh api -X GET search/issues \
    -f q='is:pr is:open author:@me label:ship:ready,ship:blocked,ship:escalated'  # correct
  ```
  (A single `--label "review:nits"` is fine — the flag handles one colon-bearing label
  correctly. It is only the OR that it cannot express.)

  `ship:blocked` exists because a gated `review:blocker` PR that an agent has already
  triaged is otherwise **indistinguishable** from one nobody has touched — same label,
  same everything. Without it the user has to read a session transcript to find out what
  is waiting on them, which is exactly the visibility failure this loop is supposed to
  remove. It is deliberately *not* a `review:*` state: it says nothing about the verdict,
  so it coexists with `review:blocker` and never touches the one-review-label invariant.

- **`/start` on `main` tells agents to ignore these labels.** Its Phase-2 notes say
  *"Do not use `review:*` labels — they exist in the taxonomy but are not applied in
  practice."* That was true when written and is now false — `/pr-watch` has been writing
  them since #498, and 30 of 73 open PRs carried one on 2026-08-04. **PR #527** rewrites
  `/start` to read the labels first and derive only as a fallback; until it merges,
  `/start` will actively route around the signal this skill produces. If `/start` seems
  blind to a PR you know is cleared, this is why.

- **Cost is the failure mode that has actually bitten this workspace.** An Opus captain
  plus five specialists under `/loop pr-watch` burned roughly **$1,381 of equivalent API
  spend in two operational days** — and the majority of it was **idle cache-write tax on
  the parent `/loop` session itself**, before any review work happened
  (`planning/archive/pre-refocus/superpowers-specs/2026-05-13-pr-review-async-design.md`).
  That is why the Leader tick is one `gh search` and a roster diff, why shipwrights are
  Sonnet, and why the heartbeat is 15–20 minutes rather than 2. **If you find yourself
  reading a diff, a source file, or a per-repo `gh pr list` in the Leader, stop — that is
  the regression.** The single-Sonnet retreat is graded "textbook Anthropic restraint" in
  `planning/2026-07-21-autonomous-review-and-graph-engineering-research.md`; don't undo it.

- **Verify on GitHub; never trust a teammate's word.** Agents here go idle *without*
  sending their final report — recorded independently in `pr-watch/GOTCHAS.md` ("alive but
  never posting a comment *or* a reply") and in the 2026-07-03 parallel-ship-pr memory
  ("expect to ping each idle agent via SendMessage to extract it"). A silent agent looks
  exactly like a working one. The label and `updated_at` from the scan are the truth; the
  teammate's summary is a convenience.

- **`idle` ≠ freed.** A shipwright that has finished still holds its pane until stopped.
  To pull the next repo off `queue[]` you must `TaskStop` the finished one. This is why
  the cap is enforced on *live* teammates, not busy ones.

- **The agent cap is a machine ceiling, not a policy.** Past ~8–9 concurrent agents
  (Leader included) spawning fails outright with `fork failed: Device not configured` — a
  hard error at spawn time, not a graceful queue. The 6-shipwright cap leaves headroom.
  Per-repo rather than per-PR is partly *why* this fits: today's queue is 16 actionable
  PRs but only 9 repos.

- **Key on the label, never on reviewer prose.** `/ship-pr` detects merge-readiness by
  matching phrases ("ready to merge", "nothing new to flag", "LGTM"). That coupling is
  fragile the moment `/pr-watch` runs on a different model or vendor — the new reviewer
  will not reliably emit those exact strings, and `/ship-pr` reads a missing phrase as
  *no verdict* and loops forever on a finished PR. Labels are written by `pr_label.py`, so
  they survive the swap. Prose is for *what* to fix; the label is for *whether*.

- **A stale `review:approved` is a real hazard.** Labels carry no SHA, so an approval can
  outlive the commit it was about if `/pr-watch` isn't running to flip it to
  `review:re-review`. Before stamping `ship:ready`, confirm the newest pr-review:v1
  `review` record's `reviewed_sha` (legacy fallback: the `<!-- pr-watch: sha=… -->`
  marker) equals `headRefOid`. This is the one place the Leader legitimately spends a
  `gh pr view` per PR.

- **An empty `statusCheckRollup` is not a green CI run.** Several repos here have no CI at
  all, so the rollup comes back empty. Treat it as shippable but **say so** — "no checks
  configured" — rather than reporting "CI green", which implies something was verified.

- **`pr_label.py set` cannot write `ship:ready`.** Only the six `review:*` states are in
  `REVIEW_STATES`; passing `ship:ready` raises `UnknownStateError` and exits 2. Apply it
  with `gh pr edit --add-label` *after* `pr_label.py ensure <repo>` has bootstrapped the
  taxonomy — `ensure` is idempotent, costs one read in steady state, and keeps the colour
  and description defined in exactly one place.

- **Push auth: SSH may not survive an agent shell.** Remotes here are `git@github.com:`
  and `SSH_AUTH_SOCK` is often unset for agents; `pr-watch/GOTCHAS.md` records pushes
  failing under biometric gating (reads and `gh` API calls are unaffected, which is why
  reviewers never noticed). A global credential helper maps `https://github.com` to
  `gh auth git-credential`, so the documented fallback is a transient rewrite that mutates
  no config:
  ```bash
  git -c url."https://github.com/".insteadOf="git@github.com:" push
  ```

- **Two writers on one branch is the real corruption risk.** `/ship-watch` and a human (or
  another agent) can both hold the same PR branch. Shipwrights re-fetch `headRefOid`
  immediately before pushing and abort if it moved — they never force-push and never
  rebase a branch they didn't create. If `git worktree add` refuses because the branch is
  checked out elsewhere, that is the guard working; report and move on.

- **Never clone into `~/Developer`.** Four repos with open PRs aren't checked out locally.
  Cloning them into the workspace tree changes the workspace's shape as a side effect of a
  review loop; where a repo lives is the user's call. Clone into `scratch_dir`.

- **`/loop ship-watch` and `/loop pr-watch` cannot share a session.** The runtime
  supersedes all pending `loop` wakeups when a new one is armed, so the second loop
  silently kills the first. Run them in separate sessions (`claude-worktree`) — which is
  also the seam that lets the reviewer run on a different model entirely.

- **The scan only sees PRs a reviewer has ruled on.** Unlabeled PRs are invisible to
  `/ship-watch` by design — 44 of 71 open PRs on the day this was written. If the queue
  looks emptier than reality, the gap is upstream: run `/pr-watch --backlog` to get them
  reviewed first. This is a coverage boundary, not a bug.

- **herdr is the documented escalation, not the default.** It would give each repo a
  genuinely independent `claude` process with its own full context window, real lifecycle
  states (`idle`/`working`/`blocked`/`done`) that beat guessing from `updated_at`, and
  cross-vendor agents. But every mutating verb (`herdr agent start|prompt`, `pane run`) is
  `ask`-gated by the local policy, which makes it wrong for an unattended loop, and it
  requires `HERDR_ENV=1`. Reach for it when you're supervising interactively or want a
  non-Claude shipwright — not for the overnight run.

- **Naming:** `ship-pr` / `ship-watch` / `pr-watch` / `review-pr` is a crowded family.
  `pr-watch/GOTCHAS.md` already defers this to marketplace graduation; don't rename
  load-bearing skills piecemeal.
