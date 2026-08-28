---
name: pr-watch
description: >
  Local reviewer half of /ship-pr. Manually invoked: scans open PRs across your GitHub
  orgs and spawns one persistent Sonnet reviewer teammate per PR to post agent feedback.
  A deliberate second opinion alongside the claude-code-action reviewer, not a substitute
  for it — run it when you want an off-CI read on a specific PR. Triggers on "pr watch",
  "watch my PRs", "review incoming PRs".
---

# PR Watch — local reviewer

The **reviewer** seat opposite `/ship-pr`. A **manually invoked** Leader scans in-scope
open PRs and dispatches each to a persistent **Sonnet reviewer teammate** that posts
feedback. One invocation is one pass — it does not schedule itself.

> **This is not the automatic reviewer.** `claude-review.yml` is, on every repo, via the
> `mriechers-pr-reviewer` App. This skill is the deliberate, local second opinion you
> reach for; nothing invokes it on your behalf. The two skills never
call each other — they ping-pong through the PR itself (a `/ship-pr` push moves the head
SHA → the Leader notices → the teammate re-reviews).

**Arguments:** `$ARGUMENTS`

| Arg | Mode |
|---|---|
| (none) | **Single pass** — scan + dispatch; backlog-confirm on the first pass if uncovered PRs exist |
| `--backlog` | **One-shot catch-up** — scan, flag uncovered PRs, confirm which to handle, dispatch |
| `--repo <owner/repo> --pr <n>` | **Scoped one-shot** review of a single PR (no team; good for testing) |
| `--preview` | **Dry run** — print what would be reviewed; post nothing |
| `--status` | Summarize live teammates + the last cycle |
| `--voice <a\|b\|c>` / `--voice off` / `--voice list` | **Modifier** (composes with any mode) — set/clear/show the review prose rotation |

The scan helper is `scripts/pr_scan.py` (stdlib only). Default owners:
`mriechers`, `public-media-work`, `Wonder-Cabinet-Productions` — pass `--owners a,b` to
override. Marker of record: `<!-- pr-watch: sha=<head> -->`.

## Detect the mode

**Step 1 — consume `--voice` first (it is a modifier, not a mode).** Before selecting a
mode, scan `$ARGUMENTS` for `--voice` and, if present, apply it to round state *and strip
it from `$ARGUMENTS`* so the leftover flags select the mode cleanly. There is no argparse
path for this — `scripts/pr_scan.py` is a scanner and never touches
`.pr-watch-state.json`; you read and write that file yourself, exactly as you already do
for `started_at`, `backlog_done`, `roster`, and `queue`.

| Form | Do this to `.pr-watch-state.json` | Then |
|---|---|---|
| `--voice "a\|b\|c"` | split on `\|`, trim each entry, drop empties → write as `voice_rotation[]`; set `voice_cursor` to `0` **only if the key is absent** (a re-set of the same or a new rotation keeps the running cursor, so variety doesn't restart mid-session) | continue to Step 2 |
| `--voice off` | delete both `voice_rotation` and `voice_cursor` | continue to Step 2 |
| `--voice list` | read only — write nothing | print the rotation with the next entry (`voice_cursor % len`) marked, then **stop**; this is terminal, it never falls through to a review |
| absent | leave both keys untouched — **never** reset them | continue to Step 2 |

Write the state file before dispatching anything. The "absent" row is the load-bearing
one: a bare `/pr-watch` carries **no arguments**, so a pass that rewrote state from empty
arguments would clear a rotation set on an earlier invocation and silently revert to the
default voice after exactly one styled review.

**Step 2 — select the mode** from what remains of `$ARGUMENTS`: `--preview` → Preview;
`--repo`+`--pr` → Scoped; `--backlog` → Backlog; `--status` → Status; otherwise → a
single pass (the default).

## Voice rotation (`--voice`)

Varies the *prose* of reviews so a long night of them doesn't read as one block. Style
only: it never changes what is found, or whether something is reported.

```
/pr-watch --voice "terse senior engineer|patient explainer, one analogy max|dry and amused"
/pr-watch --voice off      # clear it; back to the default voice
/pr-watch --voice list     # show the rotation and which entry is next
```

Pipe-separated entries become `voice_rotation[]` in the round-state file — parsed and
persisted by **Step 1 of "Detect the mode"**, which is the only place that writes these
keys. **Persisting is the whole point** — a bare `/pr-watch` carries *no arguments*, so a
flag that only lived for one invocation would style exactly one review and then silently
revert on the next run.

**Selection.** Keep a `voice_cursor` integer in round state. Before each posted review take
`voice_rotation[voice_cursor % len(voice_rotation)]`, then increment and persist. A global
cursor rather than one keyed on PR number is what actually delivers variety — it rotates
across consecutive reviews even within a single PR's thread.

Pass the selected string to the reviewer as a `voice` field in the dispatch payload; the
inline-review path (see "Running under another agent") reads it from round state directly.
With no rotation set, omit the field entirely and reviewers use their default voice.

**The latitude is real but bounded.** A voice may reframe structure — reorder sections, open
on a narrative line, weave findings into prose instead of a list. What it may never touch is
listed in `prompts/reviewer.md` § Voice: chiefly the dedup marker, the severity words, and
the merge-ready phrasing `/ship-pr` keys on. A stylish review that leaves `/ship-pr` unable
to tell "done" from "no verdict" has broken the loop it feeds.

## Preview (`--preview`)

Run and show the table, then stop — no team, no posting:
```bash
python3 scripts/pr_scan.py scan --owners mriechers,public-media-work,Wonder-Cabinet-Productions --limit 30
```
Summarize per PR: `repo #num · action · needs_attention · title`.

## Scoped one-shot (`--repo X --pr N`)

For a single PR right now, no team:
1. `python3 scripts/pr_scan.py scan` is fleet-wide; for one PR just fetch it directly and
   build the payload:
   ```bash
   gh pr view <n> --repo <X> --json number,headRefOid,state,isDraft,comments
   ```
2. If `state != OPEN` or `isDraft`, report and stop.
3. Compute `action` from the newest `<!-- pr-watch: sha= -->` marker in `comments` vs
   `headRefOid` (same rule pr_scan uses). If `current`, say "already reviewed at this SHA"
   and stop.
4. Spawn a single reviewer with `prompts/reviewer.md` as its briefing and the payload
   (`repo`, `pr_number`, `head_sha`, `action`, `last_reviewed_sha`, `skill_dir`, plus
   `voice` when a rotation is set — take the next entry per § Voice rotation, then
   increment and persist `voice_cursor`; omit the field entirely when no rotation is set),
   on Sonnet:
   ```
   Agent({ name: "rv-<owner>-<name>-<n>", model: "sonnet",
           prompt: <contents of prompts/reviewer.md> + "\n\n## This dispatch\n<payload>" })
   ```
   (Teammate names must be slash-free: build `rv-<owner>-<name>-<n>` from the repo's
   `owner/name` with every non-alphanumeric character replaced by `-`, e.g.
   `mriechers/the-lodge#12` → `rv-mriechers-the-lodge-12`.)
5. Print the resulting comment URL.

## Single pass (default)

Each pass is a fresh context — the Team persists between invocations; your in-head state
does not.

1. **Scan:**
   ```bash
   python3 scripts/pr_scan.py scan --owners <owners> --limit 30
   ```
   On the **first pass of the session**, if `.pr-watch-state.json` has no `started_at`,
   record one now (`date -u +%Y-%m-%dT%H:%M:%SZ`). It is the baseline that separates PRs
   already open when you started from PRs opened after — only the latter are auto-adopted
   in steady state.
2. **First pass / backlog present:** if any record has `needs_attention: true` AND you
   have not yet run the backlog confirmation this session, run the **Backlog** flow below
   before auto-dispatching. (Track "backlog done" in the round-state file.)
3. **Ensure the Team exists** (bootstrap once per session):
   ```
   TeamCreate({ team_name: "pr-watch", description: "PR Watch — leader + per-PR reviewers" })
   ```
   If it already exists, skip.
4. **Dispatch by action:** every payload a reviewer teammate receives — first dispatch or
   re-review alike — carries the same six required fields: `repo` (owner/name),
   `pr_number`, `head_sha`, `action` (`new` or `changed`), `last_reviewed_sha` (null on
   the first dispatch), and `skill_dir` — the absolute path to the directory containing
   this `SKILL.md` (normally the deployed `~/.claude/skills/pr-watch`; a worktree path
   during development of this skill), which the reviewer needs for its Step 6 label
   write and its Step 5 record emission.
   **Plus one optional seventh field, `voice`** — present only when `voice_rotation[]` is
   set in round state. Build it per § Voice rotation: take
   `voice_rotation[voice_cursor % len(voice_rotation)]`, then increment and persist
   `voice_cursor` **once per posted review**, so the rotation advances across teammates
   and passes rather than restarting. With no rotation set, omit the field entirely and the
   reviewer uses its default voice. This applies to **both** dispatch paths below —
   `SendMessage` re-reviews carry it too, not just first `Agent()` spawns; a re-review that
   drops the field would silently snap back to the default voice mid-thread.
   - `action == "new"` → decide whether to **adopt** (first-review) this PR:
     1. **Skip if `is_draft` or `opted_out`** — never first-review a draft or a
        `no-pr-watch` PR. Do **not** gate on `needs_attention` here: a PR the Action
        already reviewed still gets a pr-watch review, because pr-watch is the Action's
        stand-in and re-reviews the new commits the Action no longer does.
     2. **If the PR is already in the roster**, a teammate is in-flight. If that teammate
        is still alive, **skip** — do not spawn a second (prevents a duplicate review in
        the window before the first comment/marker lands). If the roster entry exists but
        the teammate is gone (crashed before posting), respawn it and dispatch.
     3. **Otherwise adopt only if it is genuinely new** — its `created_at` is later than
        the session's `started_at`. Pre-existing PRs (opened before `started_at`) are
        handled **only** through the Backlog confirmation, never auto-adopted here; this is
        what stops the first steady-state pass from flooding every open PR. (Need a
        specific pre-existing PR reviewed now? Use the scoped one-shot `--repo X --pr N`.)
     4. **To adopt:** write the roster entry immediately (before the review completes),
        then spawn `rv-<owner>-<name>-<n>` (Sonnet, briefed with `prompts/reviewer.md`) and
        send it the payload.
   - `action == "changed"` → **skip if `is_draft` or `opted_out`**, the same guard as the
     `new` branch above: a `no-pr-watch` PR must stop label writes too, not just
     first-review dispatch. Otherwise mark the prior verdict stale before dispatching:
     ```bash
     python3 scripts/pr_label.py set <repo> <n> review:re-review
     ```
     Then the PR's teammate should already exist; `SendMessage` it the new payload
     (re-review). If the teammate is gone (cold start), respawn it — the PR thread
     carries prior context. The label write needs no verdict, so it does not block the
     pass; it closes the window where a PR keeps `review:approved` while unreviewed
     commits sit on top of it.
   - `action == "current"` → skip.
   - **If a teammate reports a failed label write** (per `reviewer.md` step 6), the Leader
     runs `pr_label.py set <repo> <n> --from-severities "<severities>"` itself, translating
     that teammate's step-7 report into comma-separated severity **names** — `High,Nit`, not
     counts like "2 High, 1 Nit", and `""` when it reported clean. Keep the quotes. The
     comment's SHA marker already landed, so the PR classifies as `current` on the next scan
     and nothing else will retry the write.
5. **Retire** teammates whose PR no longer appears in the open scan (merged/closed PRs
   drop out of `--state open`), or whose record `state` is not `OPEN`. Remove them from
   the roster.
6. **Respect the cap — pace and queue, never burst.** Keep at most 8 live teammates, and
   treat it as a **hard ceiling**: spawning past ~8–9 concurrent agents fails outright
   (`fork failed: Device not configured`), so dispatch at most `cap − live` new reviewers
   this pass. Enqueue the overflow **oldest-first** in the round-state `queue[]` and `log()`
   what you deferred — never drop silently. Each subsequent pass, retiring merged/closed
   PRs (step 5) frees slots that pull from `queue[]`. To drain a queue **larger than the
   cap in a single pass** (a stacked-up backlog), recycle panes: verify a reviewer's marker
   is on its PR, `TaskStop` it, then spawn the next queued PR into the freed slot — see
   GOTCHAS → "Draining a backlog bigger than the cap".
7. **End the pass.** Report what was dispatched and stop. Reviews continue in their
   teammates; run the skill again when you want another pass. Never block waiting on a
   review to finish.

## Backlog (`--backlog`, and the first-pass gate)

1. **Scan for uncovered PRs:**
   ```bash
   python3 scripts/pr_scan.py scan --owners <owners> --limit 30 --backlog
   ```
   (`--backlog` filters to `needs_attention: true` — not draft, not opted out, no agent
   feedback yet.)
2. **Present the list** — `repo · #num · age · title` (`age` is derived from the record's
   `updated_at` field) — and **ask which to handle**.
   Accept "all", a number list ("1,3,5"), or "none". Do not auto-dispatch the backlog.
3. **For chosen PRs:** dispatch as `action == "new"` (spawn a Sonnet teammate each,
   respecting the soft cap).
4. **For declined PRs:** apply the opt-out label so they are never re-asked. Bootstrap the
   taxonomy through `pr_label.py` rather than a bare `gh label create` — the label's color
   and description have exactly one definition (in `pr_label.py`'s `TAXONOMY`), not a
   second one improvised here that could drift out of sync with it:
   ```bash
   python3 scripts/pr_label.py ensure <repo>
   gh pr edit <n> --repo <repo> --add-label "no-pr-watch"
   ```
5. Record "backlog done" in the round-state file so later passes go straight to steady state.

## Protocol records

Reviews coordinate through **pr-review:v1 records** — compact JSON in HTML-comment
markers on PR comments. The reviewer prompt (Step 5) emits a `request` + `review` pair
per round via `scripts/pr_record.py`; the scanner (`scripts/pr_scan.py`) reads the
newest record's `reviewed_sha` for dedup, falling back to the legacy
`<!-- pr-watch: sha=... -->` marker during the migration window. The full protocol —
record kinds, roles, precedence, findings lifecycle, terminal states — lives in this
repo's `planning/` unified spec. Labels are the query index; records are the evidence.
The label vocabulary has exactly one definition: `scripts/pr_label.py` `TAXONOMY`.

## Round state (survives invocations)

Persist to `<worktree-root>/.pr-watch-state.json` and read it each pass; git-exclude it via
`echo .pr-watch-state.json >> "$(git rev-parse --git-path info/exclude)"`:
```json
{ "started_at": "2026-07-16T12:00:00Z", "backlog_done": true,
  "roster": { "mriechers/the-lodge#12": "rv-mriechers-the-lodge-12" },
  "queue": ["public-media-work/pbswi#22", "mriechers/homelab#28"],
  "voice_rotation": ["terse senior engineer", "dry and amused"], "voice_cursor": 7 }
```
`voice_rotation[]` + `voice_cursor` drive `--voice`; both absent = default voice. The cursor
is a running count of styled reviews, so it keeps advancing across passes and sessions.
`queue[]` holds first-reviews that overflowed the cap (oldest-first, `repo#num`); it drains
as live slots free — see Single pass step 6 and GOTCHAS. Empty in steady state.

## Status (`--status`)

Report the current cycle without changing anything — post nothing to GitHub:
1. Read `<worktree-root>/.pr-watch-state.json` — show `backlog_done` and the roster
   (each tracked `repo#num → teammate`).
2. For each live teammate, `SendMessage` it: "status — one-line summary of your PR's
   latest review". Collect the replies. If a teammate no longer exists, mark its PR as
   "no live reviewer (will respawn on the next change)".
3. Print a compact table: `repo#num · teammate · last action · open findings`.

## Guardrails

- **Never merge, never push code.** pr-watch agents (reviewers and the Leader) post
  comments and write review-state labels only — never commits, never merges.
- **One comment per (PR, head SHA)** — the marker enforces idempotency; a `current` PR is
  never re-reviewed.
- **Self-contained:** owners + bot logins are config; no hard dependency on the-lodge.
- **Draft + `no-pr-watch` PRs are skipped** (pr_scan already filters them from
  `needs_attention`; also skip them from first-review dispatch).
- **Exactly one `review:*` label per PR.** `pr_label.py` enforces this; never add or
  remove `review:*` labels with a bare `gh pr edit`, or the invariant drifts.

## When to use / not

- **Use:** you want a local, off-CI second read on incoming PRs — a different reviewer
  with a different prompt, on demand.
- **Not because CI is unavailable.** That premise was measured false: 777 private Actions
  minutes against a 3,000/month allowance, never once exhausted. What actually stopped on
  2026-08-05 was Copilot's AI credits, which is a different product.
- **Not for:** driving your own PR to merge (that's `/ship-pr`), or merging (human-owned).

## Running under another agent (Gemini, Codex, …)

The **engine is harness-neutral**: `scripts/pr_scan.py` (Python stdlib + `gh`),
`prompts/reviewer.md`, and the `gh pr comment` marker protocol run under any agent with a
shell. Only the *orchestration* above uses Claude Code primitives — `TeamCreate` /
`Agent()` / `SendMessage` (the persistent per-PR teammates).
Without those, run the **inline** equivalent — same behavior, no teammates:

1. `python3 scripts/pr_scan.py scan --owners <owners> --limit 30` → JSON records.
2. On the first run, record `started_at` (the baseline) in `.pr-watch-state.json` yourself.
3. For each record: `current` → skip. `new` → adopt only if it's not a draft/opted-out and
   it's genuinely new (`created_at > started_at`) or already in your roster; `changed` →
   re-review. To handle one: review the diff **yourself** following `prompts/reviewer.md`
   and post one `gh pr comment` ending in `<!-- pr-watch: sha=<head> -->` (the real 40-hex
   SHA). You are the reviewer — no sub-agent needed. If `voice_rotation[]` is set in round
   state, select and advance the cursor yourself (see § Voice rotation) and apply
   `prompts/reviewer.md` § Voice — including its invariants.
4. Re-run when you want another pass. Do not wrap this in a scheduler: the automatic
   reviewer is `claude-review.yml`, and a second unattended writer on the same PR is the
   condition this skill was deliberately taken out of.

Dedup, backlog flagging, and the marker contract are identical across harnesses because
they live in `pr_scan.py` and the PR thread, not in any agent runtime. Per
`non-claude-agents.md`, do this from your own git worktree.

## Typical use

```
/pr-watch --backlog                          # clear the catch-up queue
/pr-watch --repo mriechers/the-lodge --pr 42 # one specific PR
/pr-watch                                    # one pass over what's open
```
