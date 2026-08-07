# pr-watch — GOTCHAS

- **Dedup lives in the PR thread, not a local file.** The `<!-- pr-watch: sha=<head> -->`
  marker on the newest review comment is the source of truth. Losing the round-state file
  or the Team is safe — a cold start re-derives coverage from markers.
- **`gh pr comment` uses the gh API over HTTPS**, so posting reviews is unaffected by the
  biometric-gated SSH `git push` failure that hits agent shells. Reviewers must never
  `git push`.
- **Only top-level comments carry the marker.** `pr_scan.py` reads `gh pr view --json
  comments` (issue comments) + `reviews`; it does NOT parse inline review-thread comments
  (those need GraphQL). Keep reviews as top-level comments so dedup works.
- **Backlog is confirmed once per session.** Declined PRs get the `no-pr-watch` label so
  they are never re-asked; relabel to re-include one.
- **The soft cap is also a hard machine ceiling.** ~8–9 concurrent agents (the Leader plus
  its reviewers) is not just policy — spawning past it fails *at spawn time* with
  `fork failed: Device not configured` (tmux pane / process exhaustion), a hard error, not
  a graceful queue. So never burst-dispatch a whole backlog at once; pace dispatch to at
  most `cap − live` new reviewers per tick and queue the rest (round-state `queue[]`).
- **Draining a backlog bigger than the cap — the recycle pattern.** A reviewer that has
  already posted still holds its pane until stopped: `idle` ≠ freed. To push more PRs
  through than the cap allows, recycle slots:
  1. **Confirm the review landed on the PR — don't trust the teammate's word.** A reviewer
     can go `idle` without ever relaying its summary. Verify the marker is on-thread:
     `gh pr view <n> --repo <repo> --json comments --jq '.comments[] | select(.body|test("pr-watch: sha="))'`.
  2. **`TaskStop` the confirmed-posted reviewer** to free its pane.
  3. **Spawn the next queued PR** into the freed slot; repeat until the queue drains.
  This is a one-shot-clear tactic. Under steady-state `/loop` you rarely hit it — reviewers
  post within ~2 min and free their own slots — but a built-up backlog (e.g. an Actions
  outage that stacked a day of PRs) saturates the ceiling, so pace it through `queue[]`.
- **Naming:** revisit the ship-pr/pr-watch/review-pr family at marketplace graduation
  (see the design spec's follow-ups) rather than renaming load-bearing skills now.
- **Coverage boundary.** pr-watch adopts a PR two ways: a confirmed `--backlog` pick, or
  it being opened after the loop started (`created_at > started_at`). A pre-existing PR the
  Action already reviewed is treated as covered and is NOT auto-adopted — even if new
  commits land during the outage. To force a review of any specific PR regardless of the
  baseline, use the scoped one-shot `--repo <owner/repo> --pr <n>`.
- **Inline-Leader review is the reliable default; teammates are a volume-only escalation.**
  In a long `/loop` run the named Sonnet reviewer teammates went **silent — alive but never
  posting a comment *or* a reply** (a stronger failure than the "idle without relaying" case
  in the recycle pattern above; the suspected trigger was a mid-session MCP disconnect that
  dropped the `Task*` tools). Reviewing **inline as the Leader** was both reliable and
  *better*: one context can fetch the diff, verify claims (contrast ratios, systemd
  add-vs-delete, cross-file refs) and connect a PR to an earlier review — connective tissue a
  pool of isolated teammates loses. So **review inline by default.** Only dispatch teammates
  when a single tick / `--backlog` drain has enough PRs (≈5+) that sequential inline would
  blow the turn — and even then, if a teammate's marker hasn't landed within ~2 min, the
  **Leader takes that review over inline** rather than waiting on a silent pane.
- **Scan wide — the default `--limit 30` windows out aged backlog.** PRs sorted below the top
  30 (old, low-activity) fall out of the scan *entirely*, so a genuinely-unreviewed backlog
  PR can be invisible — not merely baseline-gated. Run `pr_scan.py scan … --limit 50` and
  surface any aged uncovered PRs for a user decision rather than silently never seeing them.
- **Re-fetch the head SHA at post time — branches rebase/force-push mid-tick.** A PR's head
  can move between the scan and your comment (a force-push/rebase gives the same content a new
  SHA). Read `headRefOid` again right before posting, stamp *that* SHA, and confirm the
  content still matches what you reviewed. And always use the **full 40-hex** SHA in the
  marker — a truncated (e.g. 10-hex) marker does **not** fail the regex: `MARKER_RE`
  (`pr_scan.py:14`) accepts `[0-9a-fA-F]{7,40}`, so it matches and captures the short SHA
  happily. The break is one step later in `classify()` (`pr_scan.py:40-45`), where that
  short SHA never string-equals the full 40-hex `headRefOid` — so `last_sha != head_sha`
  is always true, the PR reads as `changed` forever, and it's re-reviewed every tick.
- **`NO-REVIEW` ≠ not-reviewed.** pr-watch writes its review to the PR **comments thread**,
  not the checks tab, so status tools that detect "reviewed" by looking for an installed CI
  review workflow under-report pr-watch-covered PRs — and `MERGEABLE + NO-REVIEW` then reads
  as a contradiction when it isn't. If you own such a detector, count a
  `<!-- pr-watch: sha=<head> -->` marker at head as "reviewed" (see the-lodge#456 / #457).
- **A green review check is not a verdict.** Don't treat the `review` / `claude-review`
  check as a sign-off. The reusable workflow runs `/code-review` with
  `pull-requests: read` and no `--comment`, so it computes findings and discards them — a
  green check means the job ran, not that the PR is clean. It also cannot write labels.
  The readable verdict is the pr-watch comment and the `review:*` label written beside it.
