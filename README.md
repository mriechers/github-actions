# github-actions

Shared, callable GitHub Actions **reusable workflows** for the `~/Developer`
workspace fleet. Public on purpose: the workflow definitions are non-sensitive
(no secrets live here — callers forward their own `CLAUDE_CODE_OAUTH_TOKEN`),
and a public host is the only way repos across separate owners/orgs can call one
shared workflow.

## Workflows

| File | Purpose | Key inputs |
|---|---|---|
| `.github/workflows/claude-review.yml` | Auto Claude review of every PR | `model` (default `sonnet`), `prompt`, `review_label` |
| `.github/workflows/claude-interactive.yml` | Interactive `@claude` on issues/PRs | `model` (default `sonnet`), `claude_args` |

## Consuming

Add a caller workflow that forwards the token and grants permissions, e.g.:

```yaml
name: Claude Code Review
on:
  pull_request:
    types: [opened, synchronize, ready_for_review, reopened, labeled]
jobs:
  review:
    permissions:
      contents: read
      pull-requests: read
      issues: read
      id-token: write
    uses: mriechers/github-actions/.github/workflows/claude-review.yml@689b8174f7a885dc201556aa56bf862bd2623207  # v1
    secrets:
      claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The `permissions:` block must live in the caller: a reusable can only *downgrade*
the token it is handed, and a repo whose default token is read-only would
otherwise strip the interactive job's write access.

## Releasing (SHA-pinned)

Consumers pin a full commit **SHA** (with a `# v1` comment), **not** the moving
tag — an unpinned third-party ref is a supply-chain risk and would let a bad push
hit every consumer at once. To ship a change:

1. Commit it to `main` here (and verify).
2. Optionally move the `v1` tag to the new commit as a human-readable pointer:
   `git tag -f v1 && git push -f origin v1`.
3. Re-pin the fleet: in `the-lodge`, bump the SHA in the two stub files under
   `scripts/claude-workflow-migration/stubs/`, then re-run `sweep.sh`. Its
   skip-check matches the target SHA, so repos on the old SHA update and current
   ones skip. **The sweep is the propagation/bump tool — no Dependabot needed.**

Rollback = re-pin the fleet to the prior good SHA and re-run the sweep. Moving the
tag alone does nothing (consumers pin the SHA), and a bad reusable never reaches
the fleet until a deliberate re-pin — that gap *is* the safety.
