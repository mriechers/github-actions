# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A public host for **callable GitHub Actions reusable workflows** (`on: workflow_call`)
consumed by the `~/Developer` fleet and org repos across separate owners. There is no
application code, package manager, or test suite — the workflow YAML *is* the product.

Public on purpose: no secrets live here (callers forward their own
`CLAUDE_CODE_OAUTH_TOKEN`), and a public host is the only way repos under different
owners can call one shared workflow.

## Blast radius: changes are fleet-wide but deferred

Consumers pin a **full commit SHA** (with a `# v1` comment), never the moving tag. So:

- A push to `main` reaches **zero** consumers. Nothing propagates until a deliberate
  re-pin. That gap is the safety mechanism — do not try to close it.
- Rollback = re-pin the fleet to the prior good SHA. It is not a revert here.
- The `v1` tag is a human-readable pointer only. Moving it changes nothing for
  consumers, and it may legitimately lag `main` (e.g. after a docs-only commit).
- Never tell a consumer to use `@v1` or `@main` — always a SHA.

## Editing the reusables

- `permissions:` belongs in the **caller**, not here. A reusable can only *downgrade*
  the token it is handed, so a repo with a read-only default token would have the
  interactive job's write access stripped. When a caller hits a permissions error, fix
  the caller's `permissions:` block — adding permissions to the reusable does nothing.
- Keep `model` defaulting to the `sonnet` alias so callers auto-track the latest
  Sonnet. Do not pin a dated model id.
- `claude-review.yml` guards `labeled` events against `inputs.review_label`; every
  other trigger always runs. Preserve that shape when touching the `if:`.

## Releasing (propagation lives in `the-lodge`)

1. Commit here and verify.
2. Optionally `git tag -f v1 && git push -f origin v1` (cosmetic pointer).
3. Bump the SHA in both stubs at
   `~/Developer/the-lodge/scripts/claude-workflow-migration/stubs/` — they must stay
   **byte-identical** to the-lodge's own workflows.
4. `DRY=1 ./sweep.sh` to preview, then the real sweep.

**`sweep.sh` and `pause-claude-actions.sh` are fleet writes — Mark runs them via `!`.
The classifier blocks agent fleet writes; never attempt them yourself.** The sweep's
skip-check matches the target SHA, so stale repos update and current ones skip. It is
the propagation and bump tool — no Dependabot.

See `/release-reusable` for the full walkthrough.

## Verification

There is no local test harness. Validate workflow YAML with `actionlint` (a
`PostToolUse` hook runs it on edits under `.github/workflows/`); end-to-end proof
requires a real PR in a consumer repo pinned to the new SHA.

## Commit conventions

Conventional Commits (`feat:`, `docs:`) with an explanatory body, plus trailers:

```
Agent: <agent name>
Machine: <hostname>
Co-Authored-By: Claude <noreply@anthropic.com>
```
