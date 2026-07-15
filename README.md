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
    uses: mriechers/github-actions/.github/workflows/claude-review.yml@v1
    secrets:
      claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The `permissions:` block must live in the caller: a reusable can only *downgrade*
the token it is handed, and a repo whose default token is read-only would
otherwise strip the interactive job's write access.

## Releasing (`@v1`)

The fleet pins `@v1` (a moving major tag). To ship a change: commit to `main`,
verify on a canary repo, then move the tag:

```bash
git tag -f v1 && git push -f origin v1
```

Rollback = move `v1` back to the prior good SHA. Breaking changes get a new
`v2` tag and a deliberate fleet re-point.
