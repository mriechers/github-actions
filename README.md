# github-actions

Shared, callable GitHub Actions **reusable workflows** for the `~/Developer`
workspace fleet. Public on purpose: the workflow definitions are non-sensitive
(no secrets live here — callers forward their own `CLAUDE_CODE_OAUTH_TOKEN`),
and a public host is the only way repos across separate owners/orgs can call one
shared workflow.

## Workflows

| File | Purpose | Key inputs |
|---|---|---|
| `.github/workflows/floor.yml` | Tier 0 floor — full-history gitleaks secrets scan | `gitleaks-config`, `fetch-depth` (default `0`) |
| `.github/workflows/claude-review.yml` | Auto Claude review with formal GitHub state | `model` (default `sonnet`), `prompt`, `review_label`, optional `github_app_id` |
| `.github/workflows/claude-interactive.yml` | Interactive `@claude` on issues/PRs | `model` (default `sonnet`), `claude_args` |

### The floor

Every non-archived repo in the fleet should call `floor.yml`. It is the Tier 0
check from the-lodge's `conventions/CI_STANDARDIZATION.md`: *don't ship
credentials*. Nothing is forwarded — gitleaks runs on the caller's own
`GITHUB_TOKEN`.

```yaml
name: Floor
on: [push, pull_request]
jobs:
  floor:
    permissions:
      contents: read
      pull-requests: write   # gitleaks comments findings on PRs
    uses: mriechers/github-actions/.github/workflows/floor.yml@<sha>
```

> **Why the floor lives here and not in the-lodge**, which is where the tier
> model is documented: the-lodge is private, and a reusable workflow in a
> private repo cannot be called by other repos unless that repo's Actions
> access is explicitly widened. A floor definition nothing can consume is not a
> floor — and that is what happened. As of 2026-08-10 exactly one repo in the
> fleet ran a secrets scan, and it did so by inlining gitleaks rather than
> calling the unreachable reusable.

## Consuming

Add a caller workflow that forwards the token and grants permissions, e.g.:

```yaml
name: Claude Code Review
on:
  pull_request:
    types: [opened, ready_for_review, reopened, labeled]
jobs:
  review:
    permissions:
      contents: read
      checks: write
      pull-requests: write
      issues: write
      id-token: write
    uses: mriechers/github-actions/.github/workflows/claude-review.yml@689b8174f7a885dc201556aa56bf862bd2623207  # v1
    secrets:
      claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The reusable only accepts `opened`, `ready_for_review`, `reopened`, and a
`labeled` event whose label matches `review_label` (default `claude-review`).
It serializes runs per PR. The `permissions:` block must live in the caller: a
reusable can only *downgrade* the token it is handed. `id-token: write` is
required by `anthropics/claude-code-action` to obtain its OIDC token.

### GitHub App publishing (recommended)

To publish the formal review, check run, and review labels as an organization
GitHub App rather than `github-actions[bot]`, create an installation token in
the reusable:

```yaml
jobs:
  review:
    permissions:
      contents: read
      checks: write
      pull-requests: write
      issues: write
      id-token: write
    uses: mriechers/github-actions/.github/workflows/claude-review.yml@<FULL_COMMIT_SHA>  # v1
    with:
      github_app_id: ${{ vars.REVIEW_APP_ID }}
      review_label: claude-review
      # prompt: Focus on public API compatibility and database migrations.
    secrets:
      claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      github_app_private_key: ${{ secrets.REVIEW_APP_PRIVATE_KEY }}
```

Install the App on each target repository with **Contents: read**, **Pull
requests: read/write**, **Issues: read/write**, and **Checks: read/write**.
Store its numeric ID in `REVIEW_APP_ID` and its PEM private key in
`REVIEW_APP_PRIVATE_KEY`. The App token is optional: existing OAuth-only callers
need no changes and use the caller's `GITHUB_TOKEN` for GitHub API publishing.
If the App is not installed for the repository or token minting otherwise fails,
the workflow records a warning and falls back to `GITHUB_TOKEN`.

Claude Code Action returns a constrained structured JSON verdict through its
`structured_output` output, validated with `--json-schema`; the workflow
parses it and deterministically publishes one formal review and one check run.
Only a successfully minted GitHub App installation token makes `approve`
become `review:approved`/`APPROVE`. GitHub Actions cannot approve a pull
request, so a missing or failed App token falls back to a `COMMENT` with an
explicit downgrade note while retaining the `review:approved` label and
successful check. Blockers remain
`review:blocker`/`REQUEST_CHANGES`, and nits or uncertainty remain
`review:nits`/`COMMENT`. It creates the review taxonomy labels if absent and
keeps exactly one `review:*` state label; it never changes `ship:ready` or
merges a PR. Before invoking Claude, it checks the PR head for a completed
`Claude autonomous review` check; an existing check skips publication and adds
an explicit job-summary message, preventing duplicate same-head reviews.

Fork PRs are always **report-only**. They never mint or expose the App token
and never check out fork code or receive the OAuth secret. The separate
read-only job never publishes reviews, check runs, or labels; it only records a
report-only summary. Same-repository checkouts disable persisted credentials,
and Claude is instructed not to execute PR code.

### Canary rollout

1. Create a test PR in one non-critical repository and call a full SHA of this
   workflow with the App ID and private-key secret.
2. Confirm the formal review, `Claude autonomous review` check, and exactly one
   `review:*` label were authored by the App installation.
3. Exercise the `claude-review` label trigger, then test a fork PR and confirm
   it is report-only.
4. Only then re-pin additional callers to the verified full SHA.

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
