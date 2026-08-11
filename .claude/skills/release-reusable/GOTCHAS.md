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
