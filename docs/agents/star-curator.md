# Star curator

`star-curator.yml` reconciles a user's starred repositories against their GitHub
star Lists. It files what the rules place unambiguously, and reports the rest.

## What it will and will not do

| Does | Does not |
|---|---|
| File a repo that is in **zero** lists, when **exactly one** rule matches | Unstar anything, ever |
| Report ambiguous stars (no rule, or several) | Create, rename, or delete a list |
| Report rot (archived upstream, long-stale) | Touch a repo a human already filed |
| Report list drift (empty, oversized) | Guess between two matching rules |

Those refusals are the design, not missing features:

- **Unstarring is not reversible in the way it looks.** Re-starring loses the
  original star date, and that date is what separates "saved in 2015" from
  "saved last week". On a large collection that stratification is the only
  thing making it legible.
- **`updateUserListsForItem` replaces membership rather than appending.** Filing
  a repo that is already in lists could silently drop it from lists a human put
  it in, and the API reports success either way. So the engine only ever touches
  repos in zero lists.
- **Naming a category is a judgement call.** A cluster of unfiled repos sharing
  a topic is reported, never auto-named.

## The rules file

Lives in the **calling** repo (default `.github/star-rules.yml`) so no personal
taxonomy is published to this repo. YAML if PyYAML is present on the runner,
otherwise JSON.

```yaml
# Repos pushed before this year count as stale.
stale_before_year: 2023
# Lists larger than this get flagged as drift.
oversize: 30

# Exempt whole lists whose members are old on purpose — an archive of a dead
# platform, a deliberately historical collection. Cheaper and more honest than
# enumerating every member in `keep`.
keep_lists:
  - Front-End Frameworks
  - The Webhook CMS (2013-2025)

# Silence rot signals for individual repos kept deliberately. An archived repo
# that is still the canonical reference for something belongs here — without it
# the report nags about the same decisions every week and stops being read.
keep:
  - opawg/user-agents          # archived, still the podcast analytics UA reference
  - PRX/publish.prx.org        # archived repo, Dovetail is alive

lists:
  # Keys must match the List name on GitHub exactly, or the rule is ignored.
  HA Projects:
    topics: [home-assistant, hacs, esphome, homeassistant]
    keywords: ["home assistant", "lovelace"]
  Glitch Art & Corruption:
    topics: [glitch, glitch-art, datamosh, pixel-sorting, crt]
  Awesome Lists:
    prefixes: [awesome-]       # matched against the repo NAME, not owner/name
    keywords: ["curated list"]
```

Three matchers, checked in order; any one hit files the repo:

- `topics` — intersection with the repo's GitHub topics. The most reliable
  signal, because topics are set deliberately by maintainers.
- `keywords` — substring against the **bare repo name** + description + topics,
  case-insensitive. The owner is deliberately excluded, for the same reason
  `prefixes` excludes it: a keyword that happened to match an owner login would
  sweep everything that owner publishes into one list.
  **Substring, not word** — so keep them long and specific. Deriving these
  automatically from a real collection produced `art` for a glitch-art list,
  which then matched `startech`, `smartmeter` and `Chartbuilder`; `rig` matched
  `Brightness` and `Soundflower-Original`. A short keyword is not a small
  mistake here, because a wrong single match files the repo rather than
  reporting it. Anything under about five characters wants to be a `topic` or a
  `prefix` instead.
- `prefixes` — against the bare repo name. Deliberately not the full
  `owner/name`, so an owner called `awesome-corp` does not sweep everything
  they publish into Awesome Lists.

A repo matching **two** lists is reported, not filed. That is the common case
for things like a curated list *of* glitch tools, and picking one is exactly the
call the engine defers.

## Setup

1. Create a PAT with `user` scope (add `repo` to see stars on private repos).
2. Store it as `STARS_TOKEN` in the calling repo's secrets. It cannot be
   `GITHUB_TOKEN` — that is a repo-scoped installation token with no user
   identity, so it cannot read or write user lists at any permission level.
3. Add the caller workflow (see the header of `star-curator.yml`).
4. **Run with `dry-run: true` first.** The rules will be wrong on the first
   pass; a dry run shows what would have been filed without doing it.

A dry run is read-only in every direction: it files nothing, and it also opens
and closes no issues. The full report goes to the job summary instead, so a
preview can never close the live report issue and discard the discussion on it.

The report label is created on first use if it does not exist — `gh issue
create --label` errors on a missing label rather than creating one, which would
otherwise fail a new consumer's first real run *after* the curation work was
already done.

## Ordering note

The engine files into lists that already exist and skips rules naming a list it
cannot find. A first-time setup means creating the lists and doing the bulk
filing once by hand; the weekly run is a maintenance loop, not a bootstrapper.

When a rule matches but names a list that does not exist yet, the report says
so specifically — `matched X, but no list of that name exists` — rather than
`no rule matched`. The two have completely different fixes, and conflating them
sends you to edit a rules file that was right all along.

## When the undocumented API breaks

The List mutations are what the GitHub web UI calls, but they are not in the
public schema docs and carry no compatibility promise. `assert_list_api()`
introspects for them before any write and fails the run if they are gone — so a
schema change shows up as a red check, not as a job that quietly files nothing
for three months.

When it fires the run does **not** abort. Filing switches off, the read path
runs to completion, and the report is produced with a banner saying filing is
disabled and why — so the ambiguity, rot and drift findings keep their value
while the engine is being fixed. The job exits non-zero afterwards, so the
check goes red.

The report still opens as an issue. The issue steps are gated on
`!cancelled()` rather than a bare `if:`, which Actions would silently AND with
`success()` — that would have skipped reporting in exactly the two cases where
a report matters most, leaving a weekly scheduled run to fail with nothing but
a red dot nobody is watching. An API failure also counts as a finding in its
own right, so a degraded run with no rot or drift still reports rather than
saying "nothing to report".

A crash before the engine reaches a verdict is different: it writes no
`has_findings` output, so nothing is opened *and* nothing is closed. The
outstanding report survives a run that never finished.

**The degraded mode covers the write mutation only.** `assert_list_api()`
introspects `updateUserListsForItem`, and filing is the part that can be
switched off while everything else still works. If GitHub changes the *read*
side instead — the `viewer.lists` or `UserList.items` shape `fetch_lists()`
depends on — there is no useful degraded run to have: without the lists, the
engine cannot tell a filed repo from an unfiled one, and a report built on
that would be worse than none. That case exits with a named
`star-curator: … -> HTTP …` message rather than a traceback, and the job
fails. Reads are essential; only writes are optional.
