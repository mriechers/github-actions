# /release-reusable — gotchas

Running log of mistakes made while releasing a reusable workflow. Appended to by
`/wrap-up` when a session turns one up.

## You cannot canary a re-pin PR on itself

**What went wrong:** On `the-lodge#569` (the rail-flip, which re-pins the caller
to a new reusable SHA) I recommended firing the canary *on the PR* by adding the
`claude-review` label — reasoning that for same-repo `pull_request` events GitHub
takes the workflow file from the PR's merge ref, so the PR's own re-pin would be
what ran. That premise is correct. The recommendation was still wrong.

`anthropics/claude-code-action` runs its own check *before* doing any work: the
workflow file must be byte-identical to the copy on the default branch. A re-pin
PR changes exactly that file, so the action skips:

```
Skipping action due to workflow validation: Workflow validation failed.
The workflow file must exist and have identical content to the version
on the repository's default branch.
Error is not retryable, giving up immediately
```

**Why it's confusing:** the skip doesn't present as "unreviewable." The action
exits ~91ms, `STRUCTURED_OUTPUT` comes back empty, `review_publish.py validate`
dies with `Expecting value: line 1 column 1 (char 0)`, the publish step is
skipped, and the PR shows a **failed check** with `mergeStateStatus: UNSTABLE`.
It reads like the reviewer ran and failed. It never started.

**Don't:** try to validate a new pin on the PR that introduces it. The canary is
always the **first hosted review after merge** — which is what the migration plan
said before I second-guessed it.

**Do:** expect workflow-touching PRs (re-pins, sweep stubs landed as PRs) to be
coverable only by the local `/pr-watch` fallback reviewer, and read their failing
review check as a skip rather than a verdict.

Ref: mriechers/github-actions#16, the-lodge#569, run `31242242452`.

## `job_workflow_sha` is verified — stop treating it as an open question

Same run settled the long-open assumption. With `protocol_ref` empty, the
"Checkout review protocol library" step resolved `github.job_workflow_sha` and
succeeded, and `.protocol/scripts/review_publish.py` actually executed. SHA-pinned
reusable calls do populate it. `protocol_ref` remains the escape hatch if it ever
regresses.

## "In scope" is not "installed" — and there is no path here for enrolling a repo

**What went wrong:** `mriechers/tv-debloat` was created 2026-09-07 and had **no
`.github/` directory at all**. All three of its open PRs classified `NO-REVIEW`
in `/start`, which read as a review backlog. It wasn't one — the reviewer had
never been installed.

The confusion came from `scope.sh` working exactly as designed. Its header
explains that scope is derived at runtime precisely so "a new repo is in scope
the moment it exists," replacing a hand-edited allowlist that failed open. That
is true, and it makes enrollment automatic. It does **not** make installation
automatic: the stubs land only when `sweep.sh` runs, and nothing triggers a
sweep on repo creation. A new repo is enrolled and uninstalled at the same
time, indefinitely, until someone releases something unrelated.

**Why it's confusing:** the symptom appears on the *consumer* repo as missing
reviews, so you go looking for a broken workflow, a bad pin, or a failed run.
There is nothing to find. The fleet tooling is healthy and the repo is
correctly in scope; the only fact is that no sweep has run since the repo
existed.

**Don't:** run the full release flow to enroll a repo. Steps 2–3 (move `v1`,
re-pin all three stubs) assume a change is shipping. With nothing to release, a
re-pin rewrites stubs across the entire fleet as a side effect of adding one
repo — a fleet-wide write to fix a single-repo omission.

**Do:** treat enrollment as its own mode — skip to step 4, preview with `DRY=1`,
hand the sweep off. The existing pin is already correct; the preview will show
every current repo as "already on target SHA" and only the new one as an
update. That shape (one update, everything else skipped) is the confirmation
that you are enrolling rather than releasing.

**Also:** keep the handoff command short enough not to wrap. The step-4 command
wrapped in the operator's terminal, split `git pull` across two lines, and
produced `command not found: pull` — with `cd` and `checkout` having already
run, so the failure looked like a git problem rather than a paste problem.
Prefer several short single-purpose lines over one long `&&` chain.

Ref: `mriechers/tv-debloat` PRs #4/#8/#9; repo created 2026-09-07T22:17Z.
