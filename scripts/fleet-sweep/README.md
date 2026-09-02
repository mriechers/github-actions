# Fleet sweep

Propagation tooling for the reusables hosted in this repo. It writes three
caller stubs into every in-scope repo across the fleet:

| Stub | Calls | Purpose |
|---|---|---|
| `claude.yml` | `claude-interactive.yml` | the `@claude` handler |
| `claude-code-review.yml` | `claude-review.yml` | the auto-reviewer |
| `floor.yml` | `floor.yml` | Tier 0 CI floor — full-history gitleaks scan |

Moved here from `the-lodge/scripts/claude-workflow-migration/` on 2026-09-02.
It lived in a repo that ADR 0008 declares superseded, was absent from that
repo's own wayfinder map, and validated its pins by querying this repo over the
network. Hosting it beside the reusables it pins makes that a local check.

## Scripts

| | |
|---|---|
| `scope.sh` | derives the in-scope repo list (read-only) |
| `sweep.sh` | writes the stubs fleet-wide — **fleet write** |
| `status.sh` | which repos carry which pin (read-only) |
| `pause.sh` | disable/enable a workflow fleet-wide — **fleet write**, reversible |

**`sweep.sh` and `pause.sh` are fleet writes. Mark runs them via `!`.** The
classifier blocks agent-run fleet writes; do not wrap or work around it.

## Releasing

```bash
DRY=1 ./scripts/fleet-sweep/sweep.sh    # preview — writes nothing
./scripts/fleet-sweep/sweep.sh          # execute (Mark, via !)
./scripts/fleet-sweep/status.sh         # confirm
```

See `/release-reusable` for the full walkthrough.

## Scope is derived, not committed

`scope.sh` enumerates the owners in `owners.txt`, drops archived repos, forks
(unless `INCLUDE_FORKS=1`), and anything matching `exclude.txt`.

Two reasons it is not a committed list:

**It fails closed.** The predecessor was a hand-edited `inscope.tsv`. It reached
49 rows against ~104 live repos, and one whole owner was never in it. A repo
missing from an allowlist is silently unenrolled, and that is indistinguishable
from one deliberately left out. Deriving from `gh repo list` inverts the
default: a new repo is in scope the moment it exists.

**It cannot be published.** 39 of `inscope.tsv`'s 49 rows named *private*
repos. This repo is public, and a private-repo inventory committed here could
not be unpublished. Deriving it at runtime under the operator's own `gh` auth
keeps it out of git.

Override for a one-off with `INSCOPE=/path/to.tsv`.

## The host repo cannot be swept

The stubs are named `claude.yml`, `claude-code-review.yml` and `floor.yml`.
This repo's own `floor.yml` **is the reusable**, not a caller — sweeping the
host would overwrite it with a stub that calls itself.

`exclude.txt` lists it, and `sweep.sh` refuses it independently. Removing the
line does not enable it.

## Preflight

`sweep.sh` aborts before writing anything unless all of these hold:

1. All three stubs pin the **same** SHA.
2. That SHA is a real commit here, reachable from `main`.
3. Each stub calls a reusable that **exists at that SHA**.
4. The pin carries every change made to a reusable.

Check 1 is the one that was missing. The predecessor compared the review and
floor stubs against each other and against `main`, but never checked the
interactive stub for currency at all — so its pin sat 21 commits stale
fleet-wide while the preflight passed (#37). `@claude` reviewed the default
branch instead of the PR head for three weeks.

Check 4 is deliberately narrower than the rule it replaces. The old check
demanded the pin equal `main`'s tip, which a docs-only commit broke — and now
that this tooling lives beside the reusables, editing `sweep.sh` would break it
too. What matters is that the pin carries every *reusable* change; docs and
tooling commits move `main` without invalidating it.

`ALLOW_STALE_PIN=1` bypasses check 4 only, for a deliberate rollback. Nothing
bypasses 1–3.

## Rollback

Re-pin the stubs to the prior good SHA and sweep again. Do not revert here and
do not move the `v1` tag — neither reaches consumers, who resolve SHAs.
