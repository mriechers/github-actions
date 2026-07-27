---
name: release-reusable
description: Walk the cross-repo release of a change to this repo's reusable workflows — commit here, optionally move the v1 tag, re-pin the fleet stubs in the-lodge, preview the sweep, and hand the fleet write off to Mark.
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

## 2. Optionally move the `v1` tag

Only if the user asked (`--tag` or explicit request). This is a cosmetic pointer —
consumers pin SHAs and will not notice.

```bash
git tag -f v1 && git push -f origin v1
```

## 3. Re-pin the fleet stubs

Both stubs live in a **different repo**:
`~/Developer/the-lodge/scripts/claude-workflow-migration/stubs/`

- `claude-code-review.yml` → pins `.github/workflows/claude-review.yml@<SHA>  # v1`
- `claude.yml` → pins `.github/workflows/claude-interactive.yml@<SHA>  # v1`

Replace the old SHA with the new one in both, keeping the `# v1` comment. Then verify
they are still **byte-identical** to the-lodge's own live workflows:

```bash
diff ~/Developer/the-lodge/scripts/claude-workflow-migration/stubs/claude-code-review.yml \
     ~/Developer/the-lodge/.github/workflows/claude-code-review.yml
diff ~/Developer/the-lodge/scripts/claude-workflow-migration/stubs/claude.yml \
     ~/Developer/the-lodge/.github/workflows/claude.yml
```

If they differ, update the-lodge's own workflows to match before continuing — a drifted
stub means the-lodge stops being a faithful sample of what the fleet gets.

Commit the stub bump in the-lodge (that repo's own commit conventions apply).

## 4. Preview, then hand off the sweep

Preview is safe and writes nothing — run it yourself:

```bash
cd ~/Developer/the-lodge && DRY=1 ./scripts/claude-workflow-migration/sweep.sh
```

Read the preview and report: which repos will be updated, which skip (already on the
target SHA), and anything unexpected.

**Then stop.** The real sweep is a fleet write across many repos and the classifier
blocks agent fleet writes. Ask Mark to run it via `!`:

```
! cd ~/Developer/the-lodge && ./scripts/claude-workflow-migration/sweep.sh
```

Do not run it, wrap it, or work around the classifier.

## 5. Confirm propagation

After Mark's sweep completes, re-run the audit to confirm every in-scope repo points at
the new SHA:

```bash
cd ~/Developer/the-lodge && ./scripts/audit-claude-automations.sh
```

Summarize: released SHA, repos re-pinned, repos skipped, anything still stale.

## Rollback

Re-pin the fleet to the prior good SHA (repeat steps 3–5 with the old SHA). Do not
revert in this repo and do not rely on moving the `v1` tag — neither reaches consumers.
