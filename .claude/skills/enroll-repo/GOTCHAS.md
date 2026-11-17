# /enroll-repo — gotchas

Running log of mistakes made while enrolling a repo. Appended to by `/wrap-up` when
a session turns one up.

## The classifier can block the read-only planner

**What went wrong:** On 2026-11-12 an agent ran `enroll-one.sh` from another repo's
session, chained after `grep`s over GA's docs. The auto-mode classifier denied it as
an unrequested write, although the script never writes to GitHub.

**Rule:** run the planner as its own command, not chained. If it is still denied,
hand the operator the line with `!` (step 3); do not reword it to get past the
classifier.
