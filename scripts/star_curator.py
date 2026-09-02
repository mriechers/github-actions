#!/usr/bin/env python3
"""Star curation engine for the star-curator reusable workflow.

Reads a caller-supplied rules file, reconciles the authenticated user's starred
repositories against their star Lists, files the unambiguous ones, and reports
everything else for a human to decide.

Deliberately does three things and no more:

  * It NEVER unstars. Unstarring loses the original star date, which is the
    only signal that distinguishes "I saved this in 2015" from "I saved this
    last week" — and that stratification is what makes a large star collection
    legible at all. Re-starring cannot restore it.
  * It NEVER creates or deletes Lists. Naming a category is a judgement call
    about how someone thinks about their own collection.
  * It only files a repo that is currently in ZERO lists, and only when exactly
    one rule matches. `updateUserListsForItem` REPLACES a repo's membership
    rather than adding to it, so touching an already-filed repo could silently
    drop it from lists a human put it in.

The List mutations used here are undocumented GitHub GraphQL — they are what
the web UI calls, but they carry no compatibility promise. `assert_list_api()`
fails the run loudly if they disappear, so a schema change surfaces as a red
check rather than as a job that quietly files nothing for three months.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Iterable

API = "https://api.github.com"
GRAPHQL = f"{API}/graphql"

# Mutations this engine depends on. Checked by introspection before any write.
REQUIRED_MUTATIONS = ("updateUserListsForItem",)


class ApiError(RuntimeError):
    pass


def _request(url: str, token: str, method: str = "GET", body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "star-curator")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        raise ApiError(f"{method} {url} -> HTTP {exc.code}: {exc.read()[:400]!r}") from exc


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    out = _request(GRAPHQL, token, "POST", {"query": query, "variables": variables or {}})
    if "errors" in out:
        raise ApiError(f"GraphQL errors: {json.dumps(out['errors'])[:600]}")
    return out["data"]


def assert_list_api(token: str) -> None:
    """Fail loudly if the undocumented List mutations have gone away."""
    data = graphql(token, "{ __schema { mutationType { fields { name } } } }")
    available = {f["name"] for f in data["__schema"]["mutationType"]["fields"]}
    missing = [m for m in REQUIRED_MUTATIONS if m not in available]
    if missing:
        raise ApiError(
            "GitHub's GraphQL schema no longer exposes: "
            + ", ".join(missing)
            + ". The star List API is undocumented and appears to have changed. "
            "Filing is disabled until the engine is updated."
        )


def fetch_stars(token: str) -> list[dict]:
    """Every starred repo, with the metadata the rules need."""
    stars: list[dict] = []
    page = 1
    while True:
        batch = _request(f"{API}/user/starred?per_page=100&page={page}", token)
        if not batch:
            break
        for r in batch:
            stars.append(
                {
                    "full_name": r["full_name"],
                    "node_id": r["node_id"],
                    "description": r.get("description") or "",
                    "language": r.get("language") or "",
                    "topics": [t.lower() for t in (r.get("topics") or [])],
                    "archived": bool(r.get("archived")),
                    "pushed_at": (r.get("pushed_at") or "")[:10],
                    "stars": r.get("stargazers_count", 0),
                }
            )
        page += 1
    return stars


LISTS_Q = """
query($after: String) { viewer { lists(first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
        id name
        items(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { ... on Repository { nameWithOwner } }
        }
    }
} } }
"""

LIST_ITEMS_Q = """
query($id: ID!, $after: String) { node(id: $id) { ... on UserList {
    items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on Repository { nameWithOwner } }
    }
} } }
"""


def fetch_lists(token: str) -> dict[str, dict]:
    """{list name: {"id": ..., "items": {full_name, ...}}}

    Both connections are paginated, and that is not defensive tidiness. The
    result is the "already filed" set the engine uses to decide what to leave
    alone. A member that fell off an unpaginated first page would look unfiled,
    so the engine would file it — and `updateUserListsForItem` replaces rather
    than appends, so it would be silently dropped from every other list it was
    in. A read cap here becomes data loss, not a missing row.
    """
    lists: dict[str, dict] = {}
    cursor = None
    while True:
        conn = graphql(token, LISTS_Q, {"after": cursor})["viewer"]["lists"]
        for n in conn["nodes"]:
            items = {i["nameWithOwner"] for i in n["items"]["nodes"] if i}
            page = n["items"]["pageInfo"]
            while page["hasNextPage"]:
                more = graphql(token, LIST_ITEMS_Q, {"id": n["id"], "after": page["endCursor"]})
                more = more["node"]["items"]
                items |= {i["nameWithOwner"] for i in more["nodes"] if i}
                page = more["pageInfo"]
            lists[n["name"]] = {"id": n["id"], "items": items}
        if not conn["pageInfo"]["hasNextPage"]:
            return lists
        cursor = conn["pageInfo"]["endCursor"]


def route(repo: dict, rules: dict) -> list[str]:
    """List names whose rules match this repo. Empty or >1 means ambiguous."""
    haystack = " ".join(
        [repo["full_name"].lower(), repo["description"].lower(), " ".join(repo["topics"])]
    )
    matches = []
    for name, spec in (rules.get("lists") or {}).items():
        if set(spec.get("topics") or []) & set(repo["topics"]):
            matches.append(name)
            continue
        if any(k.lower() in haystack for k in (spec.get("keywords") or [])):
            matches.append(name)
            continue
        # Prefixes match the repo name only. Matching the full "owner/name"
        # would let an owner called `awesome-corp` sweep everything they
        # publish into the Awesome Lists bucket.
        bare_name = repo["full_name"].split("/", 1)[-1].lower()
        if any(bare_name.startswith(p.lower()) for p in (spec.get("prefixes") or [])):
            matches.append(name)
    return matches


def rot_signals(
    stars: Iterable[dict], rules: dict, lists: dict[str, dict] | None = None
) -> list[tuple[str, str]]:
    """(full_name, reason) for repos showing decay, minus explicitly-kept ones.

    `keep_lists` exempts whole lists whose members are old on purpose — an
    archive of a dead platform, or a deliberately historical collection. Without
    it those repos are flagged every single week forever, and a report that
    always contains the same forty lines is a report nobody opens.
    """
    keep = {k.lower() for k in (rules.get("keep") or [])}
    for name in rules.get("keep_lists") or []:
        if lists and name in lists:
            keep |= {n.lower() for n in lists[name]["items"]}
    cutoff = f"{rules.get('stale_before_year', 2023)}-01-01"
    out = []
    for r in stars:
        if r["full_name"].lower() in keep:
            continue
        if r["archived"]:
            out.append((r["full_name"], "archived upstream"))
        elif r["pushed_at"] and r["pushed_at"] < cutoff:
            out.append((r["full_name"], f"no push since {r['pushed_at']}"))
    return out


def file_repo(token: str, node_id: str, list_id: str) -> None:
    graphql(
        token,
        """
        mutation($item: ID!, $lists: [ID!]!) {
          updateUserListsForItem(input: {itemId: $item, listIds: $lists}) {
            clientMutationId
          }
        }
        """,
        {"item": node_id, "lists": [list_id]},
    )


def build_report(filed, ambiguous, rot, drift, dry_run: bool) -> str:
    lines = ["## Star curation report", ""]
    if dry_run:
        lines += ["> Dry run — nothing was filed.", ""]

    if filed:
        lines += [f"### Filed automatically ({len(filed)})", ""]
        lines += [f"- `{n}` → **{lst}**" for n, lst in filed] + [""]

    if ambiguous:
        lines += [
            f"### Needs a decision ({len(ambiguous)})",
            "",
            "Starred but in no list. Rules matched nothing, or matched more than one.",
            "",
        ]
        for r, matched in ambiguous:
            why = f"matches {', '.join(matched)}" if matched else "no rule matched"
            desc = r["description"][:90] or "—"
            lines.append(f"- `{r['full_name']}` — _{why}_  \n  {desc}")
        lines.append("")

    if rot:
        lines += [
            f"### Rot signals ({len(rot)})",
            "",
            "Not acted on. Add a repo to `keep:` in the rules file to silence it.",
            "",
        ]
        lines += [f"- `{n}` — {reason}" for n, reason in rot] + [""]

    if drift:
        lines += [f"### List drift ({len(drift)})", ""] + [f"- {d}" for d in drift] + [""]

    if not any([filed, ambiguous, rot, drift]):
        lines.append("Nothing to report — every star is filed and nothing has rotted.")

    return "\n".join(lines)


def load_rules(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"star-curator: rules file not found at {path}")
    text = open(path, encoding="utf-8").read()
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return json.loads(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    token = os.environ.get("STARS_TOKEN", "")
    if not token:
        raise SystemExit("star-curator: STARS_TOKEN is empty")

    rules = load_rules(args.rules)
    assert_list_api(token)

    stars = fetch_stars(token)
    lists = fetch_lists(token)
    filed_names = {n for spec in lists.values() for n in spec["items"]}

    filed: list[tuple[str, str]] = []
    ambiguous: list[tuple[dict, list[str]]] = []

    for repo in stars:
        if repo["full_name"] in filed_names:
            continue
        matches = [m for m in route(repo, rules) if m in lists]
        if len(matches) == 1:
            target = matches[0]
            if not args.dry_run:
                file_repo(token, repo["node_id"], lists[target]["id"])
            filed.append((repo["full_name"], target))
        else:
            ambiguous.append((repo, matches))

    rot = rot_signals(stars, rules, lists)

    oversize = int(rules.get("oversize", 30))
    drift = []
    for name, spec in sorted(lists.items()):
        count = len(spec["items"])
        if count == 0:
            drift.append(f"**{name}** is empty")
        elif count > oversize:
            drift.append(f"**{name}** holds {count} repos (over {oversize})")

    report = build_report(filed, ambiguous, rot, drift, args.dry_run)
    print(report)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)

    has_findings = bool(ambiguous or rot or drift)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"has_findings={'true' if has_findings else 'false'}\n")
            fh.write(f"filed_count={len(filed)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
