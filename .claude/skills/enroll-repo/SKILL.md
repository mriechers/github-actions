---
name: enroll-repo
description: Enroll one GitHub repo in the fleet's automated PR review — plan with enroll-one.sh, then hand the operator the token, dry-run and sweep commands to run with `!`. Use when a repo was just created (the new-repo reminder hook fired), or when a repo has no Claude review and should.
---

Enroll one repo in automated PR review. Arguments (optional): $ARGUMENTS — the
`<owner/repo>`.

`GA` below is `~/Developer/github-actions`. Every write in this flow (the secret,
the sweep) is a fleet write: **you prepare and verify; the operator runs each write
with `!`.** Your own commands are reads.

## 1. Name the repo

Take `<owner/repo>` from the arguments, the reminder hook's message, or
`gh repo view --json nameWithOwner` in the repo's own directory.

Done when you hold one exact `owner/repo`.

## 2. Check GA's fleet-sweep matches `main`

The sweep runs GA's **working tree**: its `sweep.sh` and the stubs it installs. GA's
checkout often sits on a feature branch. Fetch, then compare:

```bash
git -C ~/Developer/github-actions fetch -q origin main
git -C ~/Developer/github-actions diff --quiet origin/main -- scripts/fleet-sweep/ && echo match
```

Done when it prints `match`. Otherwise stop: tell the operator which branch GA is on
and which fleet-sweep files differ (`git diff --stat origin/main -- scripts/fleet-sweep/`).
Switching GA's branch is their call; another session may own it.

## 3. Run the planner

```bash
~/Developer/github-actions/scripts/fleet-sweep/enroll-one.sh <owner/repo>
```

It is read-only. If the permission classifier blocks it, hand the operator the same
line with a `!` prefix and read its output from the conversation.

Branch on the result:

- **"Already enrolled"** (exit 0): report it; the flow is done.
- **refused** (exit 1, excluded repo, owner not in `owners.txt`, the host repo):
  relay the reason and stop. Adding an owner to `owners.txt` is a fleet-scope change
  for the operator to decide.
- **could not run** (exit 2): relay the error and stop.
- **A plan**: note the scope file path, the secret status, whether the branch is
  protected, and whether the commands carry `ALLOW_STALE_PIN=1`.

## 4. Judge a stale pin (only when `ALLOW_STALE_PIN=1` appears)

The planner lists every reusable commit since the stubs' pin. Read each one's diff
(`git -C ~/Developer/github-actions show <sha>`) and classify it **behaviour-free**
(comments, docs, whitespace) or **behavioural**.

Done when every listed commit has a verdict. All behaviour-free: recommend enrolling
on the old pin and say why in one line per commit. Any behavioural: stop and recommend
re-pinning the fleet first (`/release-reusable`); enrolling now would install a pin
the next release has to chase.

## 5. Hand the operator the writes

Copy the planner's printed commands verbatim, in two rounds. Prefix each sweep line
with `cd ~/Developer/github-actions && `, since its paths are relative to GA.

1. **The secret line (if printed) and the `DRY=1` sweep**, in one block.
2. **The real sweep**, only after the dry-run output lands in the conversation and
   you have checked that it touches only `<owner/repo>` and plans the three stubs
   (`claude.yml`, `claude-code-review.yml`, `floor.yml`).

On a protected branch the sweep opens a PR on the repo; the operator merges it.

## 6. Verify

Read-only, after the real sweep (and the merge, on a protected branch):

```bash
gh api repos/<owner/repo>/contents/.github/workflows --jq '.[].name'
gh secret list -R <owner/repo>
```

Done when all three stubs are on the default branch and `CLAUDE_CODE_OAUTH_TOKEN` is
listed. Then close with:

- PRs already open when the sweep ran are not reviewed: per PR,
  `gh pr update-branch <n> -R <owner/repo>`, then remove and re-add the
  `claude-review` label.
- Expect one false finding: the reviewer restores `.claude/`, `CLAUDE.md` and
  `.mcp.json` from the default branch, so a PR that changes them can read as
  reverting them.

`scripts/fleet-sweep/README.md` ("Enrolling one new repo") is the source of truth
for the mechanics; when it and this skill disagree, follow the README and fix this
file.
