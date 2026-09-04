---
name: release-reusable
description: Walk the release of a change to this repo's reusable workflows — commit, optionally move the v1 tag, re-pin the three fleet stubs, preview the sweep, and hand the fleet write off to Mark.
disable-model-invocation: true
---

Release a change to the reusable workflows in `mriechers/github-actions`. Arguments
(optional): $ARGUMENTS — e.g. a note on what shipped, or `--tag` to also move `v1`.

Consumers are SHA-pinned, so **nothing you do here reaches the fleet until step 4's
sweep runs.** Work through the steps in order and stop at any failure.

## 1. Confirm the change is releasable

- `git -C ~/Developer/github-actions status --short` — tree must be clean or the change
  committed. Uncommitted work is not releasable.
- `actionlint .github/workflows/*.yml` — must pass. If actionlint is missing, say so
  rather than skipping validation silently.
- Report the SHA you are about to release: `git rev-parse HEAD`.
- Note (**lands with the unification PR, mriechers/github-actions#12 — not yet
  on `main`**): once that PR merges, the review workflow checks out this
  repo's protocol library (`pr_label.py`, `review_publish.py`) at
  `github.job_workflow_sha` — the same SHA the consumer pins — so a re-pin
  moves the workflow and the label taxonomy **together**. From then on, the
  first canary after any release must confirm `job_workflow_sha` populated
  (the run's "Checkout review protocol library" step); if it ever comes up
  empty, pass the release SHA explicitly via the workflow's `protocol_ref`
  input in the stubs. Until #12 merges, `main`'s review workflow has none of
  this — skip this check.

## 2. Optionally move the `v1` tag

Only if the user asked (`--tag` or explicit request). This is a cosmetic pointer —
consumers pin SHAs and will not notice.

```bash
git tag -f v1 && git push -f origin v1
```

## 3. Re-pin the fleet stubs

All three stubs live **in this repo** at `scripts/fleet-sweep/stubs/` (moved from
the-lodge 2026-09-02):

- `claude-code-review.yml` → pins `.github/workflows/claude-review.yml@<SHA>  # v1`
- `claude.yml` → pins `.github/workflows/claude-interactive.yml@<SHA>  # v1`
- `floor.yml` → pins `.github/workflows/floor.yml@<SHA>  # v1`

Replace the SHA in all three, keeping the `# v1` comment. **They must agree** —
`sweep.sh` refuses a run where they disagree. That check did not exist before; the
interactive stub drifted 21 commits behind and `@claude` reviewed the default branch
instead of the PR head for three weeks (#37).

Also bump this repo's own caller, `.github/workflows/claude-code-review.yml`, so the
host is not a counter-example to what it ships.

The preflight is the verification step — run it and read it:

```bash
DRY=1 ./scripts/fleet-sweep/sweep.sh
```

It aborts unless the stubs agree, the SHA is reachable from `main`, each stub calls a
reusable that exists at it, and the pin carries every change made to a reusable. That
last check is narrower than "pin equals main's tip" on purpose: docs and tooling
commits move `main` without invalidating a pin.

## 4. Preview, then hand off the sweep

Preview is safe and writes nothing — run it yourself:

```bash
DRY=1 ./scripts/fleet-sweep/sweep.sh
```

Read the preview and report: which repos will be updated, which skip (already on the
target SHA), and anything unexpected.

**Then stop.** The real sweep is a fleet write across many repos and the classifier
blocks agent fleet writes. Ask Mark to run it via `!`:

```
! cd ~/Developer/github-actions && ./scripts/fleet-sweep/sweep.sh
```

Do not run it, wrap it, or work around the classifier.

## 5. Confirm propagation

After Mark's sweep completes, re-run the audit to confirm every in-scope repo points at
the new SHA:

```bash
./scripts/fleet-sweep/status.sh
```

Summarize: released SHA, repos re-pinned, repos skipped, anything still stale.

## Rollback

Re-pin the fleet to the prior good SHA (repeat steps 3–5 with the old SHA). Do not
revert in this repo and do not rely on moving the `v1` tag — neither reaches consumers.
