#!/usr/bin/env python3
"""ship-watch scan/classify helper — Python stdlib only.

The author-side mirror of pr_scan.py. Pure classification helpers (unit-tested)
plus a thin `gh` I/O layer and a JSON CLI. No third-party dependencies.

The whole workspace scan is **one** `gh search prs` call. That is deliberate:
the Leader runs this every tick inside a `/loop`, and a per-repo `gh pr list`
fan-out (or any diff read) is what makes a watcher loop expensive. Everything
the Leader needs to decide — labels, draft state, recency — comes back in that
single request. The head SHA does not, and is not needed here: only the
shipwright touches a diff, and `/ship-pr` step 1 fetches it.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# --- the handshake with /pr-watch -------------------------------------------
# The label vocabulary has exactly one definition: the sibling pr-watch skill's
# pr_label.py TAXONOMY. Import it — the asserts below make any drift an
# import-time failure instead of a silent disagreement.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "..", "..", "pr-watch", "scripts"))
import pr_label  # noqa: E402

BLOCKER_LABEL = "review:blocker"
NITS_LABEL = "review:nits"
REREVIEW_LABEL = "review:re-review"
APPROVED_LABEL = "review:approved"
INCONCLUSIVE_LABEL = "review:inconclusive"
SHIP_READY_LABEL = "ship:ready"
SHIP_BLOCKED_LABEL = "ship:blocked"
SHIP_ESCALATED_LABEL = "ship:escalated"
OPTOUT_LABELS = {"no-pr-watch"}

assert {BLOCKER_LABEL, NITS_LABEL, REREVIEW_LABEL, APPROVED_LABEL,
        INCONCLUSIVE_LABEL} <= set(pr_label.REVIEW_STATES)
assert {SHIP_READY_LABEL, SHIP_BLOCKED_LABEL,
        SHIP_ESCALATED_LABEL} <= set(pr_label.SHIP_STATES)
assert OPTOUT_LABELS <= set(pr_label.FLAG_LABELS)

# Everything that puts a PR in the human's court, kept as one query:
#   is:pr is:open author:@me label:ship:ready,ship:blocked,ship:escalated
USER_COURT_LABELS = pr_label.USER_COURT_LABELS

# Hard terminal dispositions — never re-dispatch, nothing is waiting.
# (ship:escalated is terminal for the loop but lands in the human's court.)
HARD_TERMINAL_LABELS = tuple(
    s for s in pr_label.TERMINAL_SHIP_STATES if s != SHIP_ESCALATED_LABEL)

# Verdicts we act on, worst first.
ACTIONABLE_LABELS = (BLOCKER_LABEL, NITS_LABEL)

DEFAULT_AUTHOR = "@me"
DEFAULT_LIMIT = 100
DEFAULT_ROOTS = [os.path.expanduser("~/Developer")]
# Depth to walk under each root. The workspace nests repos one level inside
# metarepos (homelab/opnsense-config, wonder-cabinet/prx-to-ghost-publisher),
# so 2 is the minimum that finds them all.
SCAN_DEPTH = 2
# Never resolve a PR's working copy to one of these — they are other sessions'
# worktrees, not the repo's primary checkout.
WORKTREE_MARKERS = ("/.worktrees/", "/.claude/worktrees/", "/.herdr/worktrees/",
                    "/.claude-squad/worktrees/")

REMOTE_URL_RE = re.compile(
    r"^(?:git@[^:]+:|(?:https?|ssh)://(?:[^@/]+@)?[^/]+/)(?P<nwo>[^/]+/[^/]+?)(?:\.git)?$")


# --- pure helpers (unit-tested) ---------------------------------------------

def label_names(pr) -> set:
    """The PR's label names, tolerating a missing or malformed labels list."""
    return {(lbl.get("name") or "") for lbl in (pr.get("labels") or [])}


def is_opted_out(pr, optout_labels=OPTOUT_LABELS) -> bool:
    lowered = {n.lower() for n in label_names(pr)}
    return bool(lowered & {x.lower() for x in optout_labels})


def verdict_of(pr):
    """The actionable verdict on this PR, or None. Worst wins."""
    names = label_names(pr)
    for label in ACTIONABLE_LABELS:
        if label in names:
            return label
    return None


def classify(pr) -> str:
    """Route a PR to a Leader action.

    Precedence matters and is not alphabetical:

    - ``done``      ship:ready is terminal-until-revoked. Checked first so a PR
                    that somehow carries both ship:ready and a stale verdict is
                    never re-dispatched — re-shipping a cleared PR is the one
                    mistake that wastes a whole agent round for nothing.
    - ``awaiting_user``
                    ship:blocked (a shipwright triaged this and posted its
                    plan) or ship:escalated (the loop stopped and a human must
                    unstick it) — either way it is in the human's court.
                    Checked before ``assign`` so a gated or escalated PR is not
                    re-dispatched (and re-notified) on every tick while it
                    waits.
    - ``terminal``  ship:parked / deferred / superseded / probe — a recorded
                    disposition; nothing is waiting on anyone. Never
                    re-dispatch.
    - ``skip``      draft, opted out, no review label at all, or
                    review:inconclusive / review:new. An unlabeled or
                    inconclusive PR means the *reviewer* has not ruled yet;
                    that is /pr-watch's backlog to clear, not ours.
    - ``assign``    review:blocker or review:nits — real work.
    - ``in_flight`` review:re-review — we already pushed and the reviewer owes
                    us a verdict. Waiting is the correct action.
    - ``clear``     review:approved with no ship:ready — nothing to fix, just
                    confirm CI is green and stamp the clearance.
    """
    names = label_names(pr)
    if SHIP_READY_LABEL in names:
        return "done"
    if SHIP_BLOCKED_LABEL in names or SHIP_ESCALATED_LABEL in names:
        return "awaiting_user"
    if names & set(HARD_TERMINAL_LABELS):
        return "terminal"
    if pr.get("isDraft") or is_opted_out(pr):
        return "skip"
    if verdict_of(pr) is not None:
        return "assign"
    if REREVIEW_LABEL in names:
        return "in_flight"
    if APPROVED_LABEL in names:
        return "clear"
    return "skip"


def gate_mode(verdict, auto_blockers=False) -> str:
    """Whether a shipwright may push unattended for this verdict.

    Policy: nits go autonomously, blockers stop after triage and wait for the
    user. `--auto-blockers` flips that in one place rather than making the
    Leader re-derive the policy in prose.
    """
    if verdict == BLOCKER_LABEL and not auto_blockers:
        return "gated"
    return "auto"


def agent_name(nwo: str) -> str:
    """Teammate name for a repo — slash-free, per pr-watch's naming rule.

    Every non-alphanumeric character becomes '-', matching the `rv-` rule so
    both fleets read the same way in a roster.
    """
    return "sw-" + re.sub(r"[^A-Za-z0-9]", "-", nwo)


def parse_remote_nwo(url: str):
    """owner/repo from a git remote URL (scp-style, https, or ssh://)."""
    match = REMOTE_URL_RE.match((url or "").strip())
    return match.group("nwo") if match else None


def origin_url(config_text: str):
    """The `url` of [remote "origin"] in a git config, or None.

    Hand-rolled rather than configparser: git config permits duplicate keys
    (several `fetch =` lines are normal), which configparser rejects under
    strict parsing, and tolerating that via strict=False buys nothing over
    fifteen lines that read plainly.
    """
    in_origin = False
    for raw in config_text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_origin = line.replace(" ", "").lower() in ('[remote"origin"]',)
            continue
        if in_origin and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "url":
                return value.strip()
    return None


def enrich(pr, local_path=None) -> dict:
    """One PR -> one Leader-facing record."""
    verdict = verdict_of(pr)
    return {
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "action": classify(pr),
        "verdict": verdict,
        "labels": sorted(label_names(pr)),
        "is_draft": bool(pr.get("isDraft")),
        "opted_out": is_opted_out(pr),
        "updated_at": pr.get("updatedAt"),
        "created_at": pr.get("createdAt"),
        "local_path": local_path,
    }


def group_by_repo(records, auto_blockers=False) -> list:
    """Collapse PR records into the per-repo work packages the Leader dispatches.

    One entry per repo == one shipwright. Repos are ordered by how much
    actionable work they carry (descending) so that when the live-agent cap
    forces a queue, the Leader spawns where the leverage is and defers the
    long tail — the repos with a single nit — rather than truncating an
    arbitrary alphabetical slice.
    """
    by_repo = {}
    for rec in records:
        nwo = rec["repo"]
        entry = by_repo.setdefault(nwo, {
            "repo": nwo,
            "agent": agent_name(nwo),
            "local_path": rec.get("local_path"),
            "clone_url": f"https://github.com/{nwo}.git",
            "assign": [], "in_flight": [], "clear": [], "done": [],
            "awaiting_user": [], "terminal": [], "skip": [],
        })
        if entry["local_path"] is None and rec.get("local_path"):
            entry["local_path"] = rec["local_path"]
        item = {k: rec[k] for k in ("number", "title", "url", "verdict", "updated_at")}
        if rec["action"] == "assign":
            item["mode"] = gate_mode(rec["verdict"], auto_blockers)
        entry[rec["action"]].append(item)

    out = []
    for entry in by_repo.values():
        for bucket in ("assign", "in_flight", "clear", "done",
                       "awaiting_user", "terminal", "skip"):
            entry[bucket].sort(key=lambda i: i["number"])
        entry["actionable"] = len(entry["assign"]) + len(entry["clear"])
        # Only `assign` work needs a checkout and a shipwright: the Leader
        # clears review:approved PRs inline over the gh API — no clone, no
        # agent. A clear-only repo must not inflate either figure.
        entry["needs_clone"] = entry["local_path"] is None and len(entry["assign"]) > 0
        out.append(entry)
    out.sort(key=lambda e: (-e["actionable"], e["repo"]))
    return out


def summarize(repos) -> dict:
    working = [r for r in repos if r["actionable"] > 0]
    done = sum(len(r["done"]) for r in repos)
    awaiting = sum(len(r["awaiting_user"]) for r in repos)
    return {
        "repos_total": len(repos),
        "repos_actionable": len(working),
        "prs_to_assign": sum(len(r["assign"]) for r in repos),
        "prs_to_clear": sum(len(r["clear"]) for r in repos),
        "prs_in_flight": sum(len(r["in_flight"]) for r in repos),
        "prs_done": done,
        "prs_awaiting_user": awaiting,
        "prs_terminal": sum(len(r["terminal"]) for r in repos),
        # The headline number: everything sitting in the human's court right
        # now, matching `label:ship:ready,ship:blocked,ship:escalated`
        # (awaiting_user covers blocked + escalated). Report it every tick —
        # a loop that ships work nobody looks at has not finished the job.
        "in_your_court": done + awaiting,
        "gated": sum(1 for r in repos for p in r["assign"] if p.get("mode") == "gated"),
        "needs_clone": sorted(r["repo"] for r in repos if r["needs_clone"]),
        # A shipwright is spawned only for `assign` work; clear-only repos are
        # handled inline by the Leader.
        "agents_required": sum(1 for r in repos if r["assign"]),
    }


# --- local checkout discovery ------------------------------------------------

def _git_config_path(entry: str):
    """Path to the git config for a checkout, or None if `entry` isn't one."""
    dotgit = os.path.join(entry, ".git")
    if os.path.isdir(dotgit):
        return os.path.join(dotgit, "config")
    if not os.path.isfile(dotgit):
        return None
    # A `.git` *file* means a worktree or submodule: "gitdir: <path>".
    try:
        with open(dotgit) as handle:
            line = handle.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(entry, gitdir))
    direct = os.path.join(gitdir, "config")
    if os.path.exists(direct):
        return direct
    # Worktrees keep config in the main repo, pointed at by `commondir`.
    commondir = os.path.join(gitdir, "commondir")
    if os.path.exists(commondir):
        try:
            with open(commondir) as handle:
                common = os.path.normpath(os.path.join(gitdir, handle.read().strip()))
        except OSError:
            return None
        return os.path.join(common, "config")
    return None


def _candidate_dirs(roots, depth=SCAN_DEPTH):
    """Directories to test for being a checkout, breadth-first to `depth`."""
    seen, frontier = [], [(os.path.expanduser(r), 0) for r in roots]
    while frontier:
        path, level = frontier.pop(0)
        if not os.path.isdir(path) or any(m in path + "/" for m in WORKTREE_MARKERS):
            continue
        if level > 0:
            seen.append(path)
        if level < depth:
            try:
                children = sorted(os.scandir(path), key=lambda e: e.name)
            except OSError:
                continue
            for child in children:
                if child.is_dir(follow_symlinks=False) and not child.name.startswith("."):
                    frontier.append((child.path, level + 1))
    return seen


def local_checkouts(roots=None, depth=SCAN_DEPTH) -> dict:
    """Map owner/repo -> local checkout path.

    Reads `.git/config` directly instead of shelling out to `git remote` once
    per candidate: the workspace has ~50 candidate directories and this runs
    every Leader tick.

    A real `.git` directory always wins over a `.git` file, so a repo is
    reported at its primary checkout rather than at whichever worktree the
    walk happened to reach first.
    """
    resolved, from_dotgit_dir = {}, set()
    for path in _candidate_dirs(roots or DEFAULT_ROOTS, depth):
        config = _git_config_path(path)
        if not config or not os.path.exists(config):
            continue
        try:
            with open(config) as handle:
                url = origin_url(handle.read())
        except OSError:
            continue
        nwo = parse_remote_nwo(url) if url else None
        if not nwo:
            continue
        is_real = os.path.isdir(os.path.join(path, ".git"))
        if nwo in resolved and (nwo in from_dotgit_dir or not is_real):
            continue
        resolved[nwo] = path
        if is_real:
            from_dotgit_dir.add(nwo)
    return resolved


# --- gh I/O ------------------------------------------------------------------

def _gh_json(args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _search_prs(author=DEFAULT_AUTHOR, limit=DEFAULT_LIMIT, state="open"):
    """The single workspace-wide request. Do not turn this into a fan-out."""
    return _gh_json([
        "search", "prs", "--state", state, "--author", author,
        "--limit", str(limit),
        "--json", "repository,number,title,url,labels,isDraft,createdAt,updatedAt",
    ])


def scan(prs, checkouts=None, auto_blockers=False, actionable_only=False,
         repo=None) -> dict:
    """Full scan: PRs -> enriched records -> per-repo work packages."""
    checkouts = {} if checkouts is None else checkouts
    if repo:
        prs = [p for p in prs if p["repository"]["nameWithOwner"] == repo]
    records = [enrich(p, checkouts.get(p["repository"]["nameWithOwner"])) for p in prs]
    repos = group_by_repo(records, auto_blockers=auto_blockers)
    summary = summarize(repos)
    if actionable_only:
        repos = [r for r in repos if r["actionable"] > 0]
    return {"summary": summary, "repos": repos}


def main(argv=None) -> int:
    """CLI: scan [--author @me] [--limit N] [--repo owner/name] [--fixture f.json]
    [--actionable-only] [--auto-blockers] [--roots a,b] [--no-local]"""
    ap = argparse.ArgumentParser(description="ship-watch scan")
    ap.add_argument("command", choices=["scan"])
    ap.add_argument("--author", default=DEFAULT_AUTHOR)
    ap.add_argument("--state", default="open")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--repo", help="restrict to one owner/name (scoped one-shot)")
    ap.add_argument("--fixture", help="JSON file with a gh-search-shaped list of PRs")
    ap.add_argument("--actionable-only", action="store_true",
                    help="emit only repos with work to do")
    ap.add_argument("--auto-blockers", action="store_true",
                    help="let review:blocker PRs ship unattended (default: gated)")
    ap.add_argument("--roots", default="",
                    help="comma-separated roots to search for local checkouts")
    ap.add_argument("--no-local", action="store_true",
                    help="skip local checkout discovery (every repo reports needs_clone)")
    args = ap.parse_args(argv)

    try:
        if args.fixture:
            with open(args.fixture) as handle:
                prs = json.load(handle)
        else:
            prs = _search_prs(args.author, args.limit, args.state)

        if args.no_local:
            checkouts = {}
        else:
            roots = [r.strip() for r in args.roots.split(",") if r.strip()]
            checkouts = local_checkouts(roots or None)

        print(json.dumps(scan(prs, checkouts,
                              auto_blockers=args.auto_blockers,
                              actionable_only=args.actionable_only,
                              repo=args.repo), indent=2))
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"gh command failed (exit {exc.returncode}): {exc.stderr or ''}\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
