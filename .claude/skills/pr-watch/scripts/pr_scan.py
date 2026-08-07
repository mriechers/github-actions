#!/usr/bin/env python3
"""pr-watch scan/classify helper — Python stdlib only.

Pure classification helpers (unit-tested) plus a thin `gh` I/O layer and a
JSON CLI (Task 3). No third-party dependencies.
"""

import argparse
import json
import re
import subprocess
import sys

import pr_record

MARKER_RE = re.compile(r"<!--\s*pr-watch:\s*sha=([0-9a-fA-F]{7,40})\s*-->")
OPTOUT_LABELS = {"no-pr-watch"}
BOT_LOGINS = {"github-actions[bot]", "claude[bot]", "claude"}
DEFAULT_OWNERS = ["mriechers", "public-media-work", "Wonder-Cabinet-Productions"]


def format_marker(sha: str) -> str:
    """The dedup marker appended to every posted review."""
    return f"<!-- pr-watch: sha={sha} -->"


def parse_last_reviewed_sha(comments):
    """Return the newest reviewed SHA across comments, else None.

    pr-review:v1 review records win; the legacy pr-watch marker is a
    migration-window fallback and is never emitted by updated writers.
    """
    bodies = [(c.get("body") or "") for c in comments]
    records, _ = pr_record.parse_records(bodies)
    reviews = [r for r in records if r.get("kind") == "review"]
    if reviews:
        return reviews[-1]["reviewed_sha"]
    shas = []
    # Relies on comments arriving in chronological order (gh returns oldest-first); last marker wins.
    for body in bodies:
        shas.extend(MARKER_RE.findall(body))
    return shas[-1] if shas else None


def is_opted_out(pr, optout_labels=OPTOUT_LABELS) -> bool:
    names = {(lbl.get("name") or "").lower() for lbl in pr.get("labels", [])}
    return bool(names & {x.lower() for x in optout_labels})


def classify(head_sha, last_sha) -> str:
    if not last_sha:
        return "new"
    if len(last_sha) == 40:
        return "current" if last_sha == head_sha else "changed"
    # Truncated marker: a short SHA can never string-equal the full head,
    # which used to re-classify the PR as "changed" — and re-review it —
    # every tick, forever. Prefix-match instead; >=7 hex chars makes a
    # false prefix collision negligible.
    return "current" if head_sha.startswith(last_sha) else "changed"


def has_agent_feedback(comments, reviews, bot_logins=BOT_LOGINS) -> bool:
    """True if a pr-watch marker OR any comment/review by a known review bot exists."""
    if parse_last_reviewed_sha(comments) is not None:
        return True
    for item in list(comments) + list(reviews):
        login = (item.get("author") or {}).get("login", "")
        if login in bot_logins:
            return True
    return False


def needs_attention(pr) -> bool:
    """A backlog PR needs attention when it is not a draft, not opted out,
    and has no agent feedback yet."""
    if pr.get("isDraft"):
        return False
    if is_opted_out(pr):
        return False
    return not has_agent_feedback(pr.get("comments", []), pr.get("reviews", []))


def enrich(pr, detail) -> dict:
    """Enrich a PR with classification and backlog flags."""
    comments = detail.get("comments", [])
    reviews = detail.get("reviews", [])
    head = detail.get("headRefOid", "")
    last = parse_last_reviewed_sha(comments)
    flag_input = {
        "isDraft": pr.get("isDraft"),
        "labels": pr.get("labels", []),
        "comments": comments,
        "reviews": reviews,
    }
    return {
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "head_sha": head,
        "last_reviewed_sha": last,
        "action": classify(head, last),
        "is_draft": bool(pr.get("isDraft")),
        "opted_out": is_opted_out(pr),
        "needs_attention": needs_attention(flag_input),
        "state": detail.get("state"),
        "created_at": pr.get("createdAt"),
        "updated_at": pr.get("updatedAt"),
    }


def scan(prs, detail_fn, backlog=False):
    """Scan a list of PRs, enrich each with detail, optionally filter to backlog."""
    records = [enrich(pr, detail_fn(pr["repository"]["nameWithOwner"], pr["number"]))
               for pr in prs]
    if backlog:
        records = [r for r in records if r["needs_attention"]]
    return records


def resolve_owners(raw: str) -> list:
    """Parse a comma-separated --owners value; fall back to DEFAULT_OWNERS when empty."""
    owners = [o.strip() for o in raw.split(",") if o.strip()]
    return owners or list(DEFAULT_OWNERS)


def _gh_json(args):
    """Run a gh command and return parsed JSON output."""
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _search_args(owners, state, limit):
    """Argv for the PR search — pure, so the flag set is testable.

    --archived=false is load-bearing: PRs in archived repos are unreviewable
    (label writes 403, so they cannot even carry the no-pr-watch opt-out) and
    would sit in the backlog as permanently un-drainable work.
    """
    args = ["search", "prs", "--state", state, "--limit", str(limit),
            "--archived=false",
            "--json", "number,title,url,repository,isDraft,labels,createdAt,updatedAt"]
    for owner in owners:
        args += ["--owner", owner]
    return args


def _search_prs(owners, state, limit):
    """Search for PRs by owner and state."""
    return _gh_json(_search_args(owners, state, limit))


def _pr_detail(nwo, number):
    """Fetch detailed PR info (head ref, comments, reviews)."""
    return _gh_json(["pr", "view", str(number), "--repo", nwo,
                     "--json", "headRefOid,state,comments,reviews"])


def main(argv=None) -> int:
    """CLI entry point: scan [--owners a,b] [--limit N] [--state open] [--fixture f.json] [--backlog]"""
    ap = argparse.ArgumentParser(description="pr-watch scan")
    ap.add_argument("command", choices=["scan"])
    ap.add_argument("--owners", default="")
    ap.add_argument("--state", default="open")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--fixture", help="JSON file with {'search': [...], 'details': {...}}")
    ap.add_argument("--backlog", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.fixture:
            with open(args.fixture) as fh:
                fx = json.load(fh)
            prs = fx["search"]
            details = fx.get("details", {})
            detail_fn = lambda nwo, n: details[f"{nwo}#{n}"]  # noqa: E731
        else:
            owners = resolve_owners(args.owners)
            prs = _search_prs(owners, args.state, args.limit)
            detail_fn = _pr_detail

        print(json.dumps(scan(prs, detail_fn, backlog=args.backlog), indent=2))
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"gh command failed (exit {exc.returncode}): {exc.stderr or ''}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
