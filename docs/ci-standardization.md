# CI Standardization

> **Provenance.** Moved from `mriechers/the-lodge` `conventions/CI_STANDARDIZATION.md` at `08476e8`
> (last content change `61f75d1`, 2026-08-24), under machine-ops#44 (T-12, rule 7) and machine-ops#121
> (T-31). It defines the tiers the reusable workflows in this repo implement. Edits on arrival: the
> reusable-workflow table and the caller snippets now point at this repo (the-lodge's copies were
> never callable, and `python-ci.yml` / `js-ci.yml` moved here on 2026-08-11); the propagation script
> is named by its current home (`scripts/fleet-sweep/`); the rollout-plan links into the-lodge's
> `planning/` are left as history. Everything else is as written.

Workspace-wide convention for how repos under `~/Developer/` should be CI-gated. Defines a tiered model and the reusable GitHub Actions workflows that implement it.

**Last updated**: 2026-05-08
**Rollout plan**: the-lodge `planning/ci-standardization-plan.md` (archived history)
**Sprint**: the-lodge `planning/ci-standardization-features.json` (archived history)

## Why standardize

Consistent CI signal across repos lets the automated PR review (the `pr-review` Action) and humans make merge decisions confidently — they know what "CI green" actually means. Inconsistent CI is theatrical: the gate exists but doesn't gate anything specific.

This convention defines a **floor** (universal safety), a **Tier A** suite (full quality bar for actively-developed code repos), and a **Tier B** designation (floor-only for hobby / config / one-shot repos).

## The tiers

### Tier 0 — Floor (every non-archived repo)

Universal-applicable, language-agnostic, near-zero cost.

| Check | Tool | Cost |
|---|---|---|
| `secrets-scan` | gitleaks-action (full history scan) | ~5s / push |
| `dependabot` | `.github/dependabot.yml` | passive (opens PRs) |

Even YAML-config repos and dotfiles benefit from this. No code-quality gates here — just *don't ship credentials, don't ignore CVEs*.

### Tier A — Full suite (actively-maintained code repos)

Floor +

| Check | Tool | Mode |
|---|---|---|
| `lint` | ruff (Python) / eslint (JS/TS) | **errors-only** by default (`--select=E,F,W` / `--quiet`) |
| `format` | ruff format / prettier | pre-commit hook (NOT a CI gate — keeps diffs clean locally) |
| `test` | pytest / vitest | only where tests exist |
| `typecheck` | pyright / tsc | **permissive default** (`basic` mode); strict per-file via opt-in (`# pyright: strict`) |

### Tier B — Floor only (config / hobby / one-shot)

Floor only. No lint/test/typecheck. Examples: HA YAML, ROM scripts, dotfiles, one-shot Apple Automator workflows.

The cost-benefit just isn't there for code Mark touches twice a year.

## Reusable workflows

> **A reusable workflow must live in a repo the caller can read.** `the-lodge` is
> **private**, and a reusable workflow in a private repo cannot be called by another
> repo unless that repo's Actions access level is widened (it is `none`). Every
> `uses: mriechers/the-lodge/...` line below was therefore unresolvable for the whole
> fleet — the standard was unadoptable, not merely unadopted. Measured 2026-08-11:
> **1 of 49 in-scope repos ran a secrets scan**, and that one inlined gitleaks rather
> than calling the reusable.
>
> All three reusables now live in this public repo and are propagated by
> `scripts/fleet-sweep/sweep.sh` (`python-ci.yml` and `js-ci.yml` moved here on
> 2026-08-11 with zero callers fleet-wide).

| File | Host | Calls | Use for |
|---|---|---|---|
| `floor.yml` | **`mriechers/github-actions`** (public) | gitleaks | Tier 0 / Tier B repos |
| `python-ci.yml` | **`mriechers/github-actions`** (public) | gitleaks + ruff + pyright + pytest | Tier A Python repos |
| `js-ci.yml` | **`mriechers/github-actions`** (public) | gitleaks + eslint + tsc + vitest | Tier A JS/TS repos |

### Consuming a reusable

**Floor-only (Tier 0 / B)** — you do not normally hand-write this. The floor is swept
to every repo of every owner in `scripts/fleet-sweep/owners.txt` as
`.github/workflows/floor.yml` (opt a repo out in `exclude.txt`); the stub under
`scripts/fleet-sweep/stubs/` is the sample the fleet receives. For reference, the
swept file is:

```yaml
name: Floor
on: [push, pull_request]
jobs:
  floor:
    permissions:
      contents: read
      pull-requests: write   # gitleaks comments findings on PRs
    uses: mriechers/github-actions/.github/workflows/floor.yml@<sha>  # v1
```

Pin a SHA, never `@main` — an unpinned third-party ref lets one bad push hit every
consumer at once. See this repo's `README.md` → *Releasing* for the release + re-pin
procedure.

**Python (Tier A):**

```yaml
name: CI
on: [push, pull_request]
jobs:
  python-ci:
    uses: mriechers/github-actions/.github/workflows/python-ci.yml@<sha>  # v1
    with:
      python-version: "3.12"          # default 3.11
      test-paths: "tests/ services/*/tests/"   # default tests/
      install-cmd: "uv sync --all-packages"    # default pip install -e .
      run-tests: true                  # set false for repos w/o tests yet
```

**JS/TS (Tier A):**

```yaml
name: CI
on: [push, pull_request]
jobs:
  js-ci:
    uses: mriechers/github-actions/.github/workflows/js-ci.yml@<sha>  # v1
    with:
      node-version: "22"               # default 20
      install-cmd: "pnpm install --frozen-lockfile"
      test-cmd: "pnpm test"
```

### Inputs reference

See each workflow file's `workflow_call.inputs` block for the full surface. Notable defaults:

- **ruff**: `--select=E,F,W` — errors and warnings only, no PEP 8 nitpicking. Override via `ruff-select` input.
- **pyright**: `basic` mode (permissive). For per-file strictness, add `# pyright: strict` at the top of new files. Override the global default via `pyright-mode: "strict"` input.
- **eslint**: `--quiet` — errors only.
- **tsc**: `--noEmit` — typecheck without producing build output.

## Opting out / customizing per repo

Three escape hatches:

1. **Skip the test job** — pass `run-tests: false` to `python-ci.yml` / `js-ci.yml`. Useful for repos that have lint/typecheck but no tests yet.
2. **Override commands** — every step has an input for the command. Pass your own to use a different runner (jest instead of vitest, hatch instead of pip, etc.).
3. **Custom `.gitleaks.toml`** — add the file to your repo and pass its path as `gitleaks-config`.

If you need behavior outside these knobs, **don't** copy-paste the workflow content into your repo (drift trap). Either:

- Open a PR against this convention to add the input you need, OR
- Add a separate workflow alongside the reusable for the repo-specific job.

## Tier topics on GitHub

Each repo gets a GitHub topic indicating its tier:

- `tier-floor` — Floor only
- `tier-a` — Full Tier A suite

Applied via the `tag-repos` skill. The `audit-ci` skill reads these topics to report compliance vs. expectations.

## Pre-commit (format + light lint)

Format checks live at the **pre-commit** layer, not in CI:

- It's where they belong (catch before push, not after)
- Avoids "CI failed because you added a trailing space" noise
- Keeps git diffs stable — incidental whitespace doesn't show up

Standard `.pre-commit-config.yaml` template (TODO — to be added in Tier A rollout):

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.5.0
    hooks:
      - id: ruff-format
      - id: ruff
        args: [--fix, --select=E,F,W]
```

## Branch protection (Tier A repos)

For Tier A repos, the consumer's CI workflow should be a **required check** on `main` branch protection. Default plan: yes, gate `main` on `python-ci` / `js-ci` jobs passing.

This makes the gate real — you can't bypass it accidentally.

## Rollout policy

Existing repos get migrated according to the rollout plan (`planning/ci-standardization-plan.md`):

- **Phase 2**: Floor everywhere via mass PR (`audit-ci` enumerates non-archived non-fork repos)
- **Phase 3**: Tier A on the ~10 actively-maintained code repos
- **Phase 4**: Tier topics + audit-ci v3 compliance reporting

**New repos default to Tier A** via `modernize-repo` skill (when run, it adds the appropriate caller workflow based on repo language).

## Related

- `README.md` — the reusables this repo hosts, consuming them, and releasing (SHA-pinned)
- `scripts/fleet-sweep/` — propagation to the fleet
- `.github/workflows/claude-review.yml` — the Claude Code Action review reusable (opened / ready_for_review / reopened + `claude-review` label)
