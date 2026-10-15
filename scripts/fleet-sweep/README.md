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
| `enrollment-audit.sh` | which repos never got the stubs at all (read-only) |
| `enroll-one.sh` | prepare one new repo's enrollment; prints the commands (read-only) |
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

## Enrollment is not automatic — only scope is

`scope.sh` puts a new repo in scope the moment it exists. The stubs still only
land when `sweep.sh` runs, and nothing triggers a sweep on repo creation, so a
new repo is enrolled and uninstalled at the same time until someone releases
something unrelated. `mriechers/tv-debloat` sat that way for four days and was
found by accident.

`enrollment-audit.sh` closes the *noticing* half of that gap. It is read-only,
costs one directory listing per repo, and reports repos with none of the stubs,
repos with some, repos that have all three but no `CLAUDE_CODE_OAUTH_TOKEN`
(inert -- the workflows run and error), and sweep PRs left open on protected
repos. `.github/workflows/
fleet-enrollment-audit.yml` runs it weekly and files a single issue when there
is anything to report.

The *acting* half stays manual on purpose. A scheduled job that re-pins ~89
repos is exactly what the "Mark runs it via `!`" gate exists to prevent, so the
audit never installs anything — it only says what is missing.

## Enrolling one new repo

Use this when a new repo needs review now rather than at the next release.

```bash
scripts/fleet-sweep/enroll-one.sh <owner/repo>
```

It is read-only. It looks up the default branch and whether it is protected,
reports which stubs and which secret are missing, writes a one-line scope file
under `$TMPDIR`, and prints the commands for Mark to run with `!`:

1. **The token.** `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <repo> --body "$(get-secret.sh CLAUDE_CODE_OAUTH_TOKEN)"`.
   Callers forward their own token, and the sweep does not install it. Without
   it the workflows start and then error.
2. **A dry run, then the real sweep, scoped to that one file** with
   `INSCOPE=<tsv>`, so no other repo is touched. On an unprotected default branch
   the sweep commits the stubs; on a protected one it opens a PR to merge.

**The stale-pin judgment call.** When a reusable changed after the stubs' pin,
the preflight refuses (check 4), and `enroll-one.sh` adds `ALLOW_STALE_PIN=1` to
the printed commands and lists every reusable commit since the pin. The flag is
meant for rollbacks. Enrolling on the old pin is safe only when every listed
commit is behaviour-free, such as a comments-only change: the new repo then
carries the same pin as most of the fleet. If any listed change alters
behaviour, re-pin the fleet first (CLAUDE.md, *Releasing*).

**PRs opened before the sweep are not reviewed.** Their merge ref predates the
stubs. For each one, run `gh pr update-branch <n> -R <repo>` (a server-side
merge, not a force-push), then remove and re-add the `claude-review` label.

**One false finding to expect.** The reviewer restores `.claude/`, `CLAUDE.md`
and `.mcp.json` from the default branch, because the PR head is untrusted. A PR
that adds or changes those files can therefore read as deleting or reverting
them. A repo whose skills live entirely under `.claude/` meets this on every PR
that touches them.

A Claude Code hook prints a reminder pointing here whenever a session creates
a repo. It only reminds; enrolling stays a human-run step.

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
