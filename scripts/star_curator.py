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

That degraded mode covers the WRITE mutation only. Filing can be switched off
while the read path still produces a useful report. A read-side schema change
cannot degrade: without the lists there is no way to tell a filed repo from an
unfiled one, and a report built on that would be worse than none — so it exits
with a named error instead.
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


class ListApiGone(ApiError):
    """The undocumented List mutations are missing from the schema.

    Distinct from ApiError because it is the ONLY condition that should
    trigger degraded mode. A 401 from an expired PAT is also an ApiError, and
    catching the base class reported a dead token as "GitHub changed their
    schema" — sending someone to rewrite the engine when they needed to
    rotate a secret.
    """


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
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        # HTTPError is a subclass, so this only sees transport failures —
        # DNS, TLS, timeouts. Without it a network blip is the one path that
        # escapes as a raw traceback.
        raise ApiError(f"{method} {url} -> {exc.reason}") from exc


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
        raise ListApiGone(
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
    # The bare repo name, never "owner/name". Including the owner let a keyword
    # match on the owner login and sweep everything that owner publishes into
    # one list — the exact hazard `prefixes` was already careful to avoid, let
    # back in through the other door. Route on what a repo *is*, not on who
    # happens to publish it.
    bare_name = repo["full_name"].split("/", 1)[-1].lower()
    topics = set(repo["topics"])
    haystack = " ".join([bare_name, repo["description"].lower(), " ".join(repo["topics"])])
    matches = []
    for name, spec in (rules.get("lists") or {}).items():
        # Lowercase the rule side, as keywords and prefixes already do. Repo
        # topics arrive lowercased, so a capitalised `topics:` entry matched
        # nothing at all — and the failure mode is silence: the repo lands in
        # "needs a decision" and the rules file looks fine.
        if {t.lower() for t in (spec.get("topics") or [])} & topics:
            matches.append(name)
            continue
        if any(k.lower() in haystack for k in (spec.get("keywords") or [])):
            matches.append(name)
            continue
        if any(bare_name.startswith(p.lower()) for p in (spec.get("prefixes") or [])):
            matches.append(name)
    return matches


def filing_target(matched: list[str], present: list[str]) -> str | None:
    """The one list to file into, or None if this repo needs a human.

    BOTH conditions have to hold. Gating on `present` alone meant a repo
    matching two rules got filed the moment only one of those two lists
    happened to exist yet — which is exactly the "guess between two matching
    rules" this engine documents itself as never doing. Two rules matching is
    ambiguous on its own terms; which lists exist has no bearing on it.
    """
    if len(matched) == 1 and len(present) == 1:
        return present[0]
    return None


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
        # A missing pushed_at exempts the repo rather than flagging it. The
        # date is absent, not old, and guessing either way would put a repo in
        # a report on the strength of a field GitHub did not return.
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


def _wrote(entry: tuple[str, str, bool]) -> bool:
    """Did this filed entry actually reach the API?

    A `filed` entry is (full_name, list_name, written). Carrying the flag on
    the entry rather than deriving it from position lets build_report answer
    the question from its own arguments — the alternative depended on an
    ordering invariant enforced in a different function 150 lines away.
    """
    return entry[2]


def build_report(
    filed,
    ambiguous,
    rot,
    drift,
    dry_run: bool,
    list_api_error: str = "",
    filing_error: str = "",
) -> str:
    lines = ["## Star curation report", ""]
    if filing_error:
        lines += [
            f"> **Filing stopped after {sum(1 for e in filed if _wrote(e))} "
            f"of {len(filed)} repos.**",
            f"> {filing_error}",
            ">",
            "> The writes that succeeded are committed — those repos are filed.",
            "> The rest are listed below and were not written. Re-running is",
            "> safe: an already-filed repo is skipped.",
            "",
        ]
    if list_api_error:
        lines += [
            "> **Filing is disabled — the List API check failed.**",
            f"> {list_api_error}",
            ">",
            "> Everything below is read-only and still accurate; treat it as",
            "> advisory until the engine is updated.",
            "",
        ]
    if dry_run:
        lines += ["> Dry run — nothing was filed.", ""]

    if filed:
        # Past tense only when something actually happened. A dry run and a
        # degraded run both leave the lists untouched, so "Filed automatically"
        # there contradicts the banner three lines above it — and the dry run
        # is what the docs tell a first-time user to trust.
        # A partial failure does not un-write the repos that succeeded before
        # it, so past tense is right whenever anything landed.
        heading = "Filed automatically" if any(_wrote(e) for e in filed) else "Would file"
        lines += [f"### {heading} ({len(filed)})", ""]
        for entry in filed:
            n, lst = entry[0], entry[1]
            # Each entry carries whether it was actually written. Inferring it
            # from position relative to a count meant this function depended on
            # an ordering invariant enforced 150 lines away in main().
            note = "" if _wrote(entry) else "  _(not written)_"
            lines.append(f"- `{n}` → **{lst}**{note}")
        lines.append("")

    if ambiguous:
        lines += [
            f"### Needs a decision ({len(ambiguous)})",
            "",
            "Starred but in no list. Rules matched nothing, or matched more than one.",
            "",
        ]
        for r, matched, present in ambiguous:
            missing = [m for m in matched if m not in present]
            if not matched:
                why = "no rule matched"
            elif not present:
                # The rule fired, but names a list that does not exist yet.
                # Reporting this as "no rule matched" sends someone to debug
                # their rules file when the actual fix is to create the list —
                # and first-time setup is exactly when this happens.
                why = f"matched {', '.join(matched)}, but no list of that name exists"
            elif missing:
                # Two rules matched and only one list exists. Naming just the
                # surviving one reads as a single unambiguous match and leaves
                # the reader wondering why it was not filed — the second rule
                # IS the reason.
                why = (
                    f"matches {', '.join(present)}, and also "
                    f"{', '.join(missing)}, which does not exist"
                )
            else:
                why = f"matches {', '.join(present)}"
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

    if not any([filed, ambiguous, rot, drift, list_api_error, filing_error]):
        lines.append("Nothing to report — every star is filed and nothing has rotted.")

    return "\n".join(lines)


def load_rules(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"star-curator: rules file not found at {path}")
    text = open(path, encoding="utf-8").read()
    try:
        import yaml  # type: ignore

        parse, err = yaml.safe_load, yaml.YAMLError
    except ImportError:
        parse, err = json.loads, ValueError

    # A malformed rules file is an ordinary thing to do by hand, so it gets a
    # named error rather than a traceback. Same for a file that parses but
    # isn't a mapping — a bare list is the natural wrong shape to write here.
    try:
        rules = parse(text) or {}
    except err as exc:
        raise SystemExit(f"star-curator: could not parse {path}: {exc}") from exc
    if not isinstance(rules, dict):
        raise SystemExit(
            f"star-curator: {path} must be a mapping with a `lists:` key, "
            f"got {type(rules).__name__}"
        )
    lists = rules.get("lists")
    if lists is not None and not isinstance(lists, dict):
        # Writing `lists:` as a sequence is the natural wrong guess. Caught
        # here rather than in route(), which runs once per starred repo.
        raise SystemExit(
            f"star-curator: `lists:` in {path} must be a mapping of "
            f"list name to rules, got {type(lists).__name__}"
        )

    # Every matcher is validated as a list of strings, and this is the one
    # piece of validation that is a SAFETY control rather than a courtesy.
    # `keywords: home assistant` — a bare scalar, the most natural YAML slip
    # there is — leaves Python iterating the string character by character.
    # Single-character "keywords" like `e` and ` ` substring-match nearly every
    # repo, so the rule matches almost everything, and a single match FILES.
    # On a real run that mass-mis-files a collection into one list, and
    # `updateUserListsForItem` replaces membership rather than appending, so
    # it also strips those repos out of wherever they belonged.
    for name, spec in (lists or {}).items():
        if not isinstance(spec, dict):
            raise SystemExit(
                f"star-curator: rules for `{name}` in {path} must be a mapping "
                f"of matcher to values, got {type(spec).__name__}"
                + (" (a list name with nothing under it)" if spec is None else "")
            )
        for matcher in ("topics", "keywords", "prefixes"):
            values = spec.get(matcher)
            if values is None:
                continue
            if not isinstance(values, (list, tuple)) or isinstance(values, str):
                message = (
                    f"star-curator: `{matcher}` for `{name}` in {path} must be "
                    f"a list, got {type(values).__name__}."
                )
                if isinstance(values, str):
                    # By far the likeliest mistake, so name the exact fix.
                    message += f" Write `{matcher}: [{values!r}]`."
                raise SystemExit(message)
            bad = [v for v in values if not isinstance(v, str)]
            if bad:
                raise SystemExit(
                    f"star-curator: `{matcher}` for `{name}` in {path} must "
                    f"contain only strings; found {bad[0]!r}."
                )

    oversize = rules.get("oversize")
    if oversize is not None:
        try:
            int(oversize)
        except (TypeError, ValueError):
            raise SystemExit(
                f"star-curator: `oversize` in {path} must be a number, "
                f"got {oversize!r}."
            ) from None

    return rules


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

    # The docs promise a degraded mode when the undocumented List API goes
    # away: filing off, report still produced and treated as advisory. Raising
    # here killed the run before a single star was read, so that promise was
    # never kept. Capture it, disable filing, finish the read-only work, and
    # exit non-zero at the very end so the check still goes red.
    list_api_error = ""
    try:
        assert_list_api(token)
    except ListApiGone as exc:
        # Only a genuinely missing mutation degrades. Any other API failure —
        # an expired token, a rate limit — propagates to the handler in
        # __main__ and is named for what it is.
        list_api_error = str(exc)
        print(f"star-curator: {list_api_error}", file=sys.stderr)

    may_file = not args.dry_run and not list_api_error

    stars = fetch_stars(token)
    lists = fetch_lists(token)
    filed_names = {n for spec in lists.values() for n in spec["items"]}

    filed: list[tuple[str, str, bool]] = []
    ambiguous: list[tuple[dict, list[str], list[str]]] = []
    filing_error = ""

    for repo in stars:
        if repo["full_name"] in filed_names:
            continue
        # Both sets are carried into the report: what the rules matched, and
        # which of those lists actually exist. Collapsing them loses the
        # difference between "your rules say nothing about this" and "your
        # rules are right but the list isn't created yet".
        matched = route(repo, rules)
        present = [m for m in matched if m in lists]
        target = filing_target(matched, present)
        if target:
            wrote = False
            if may_file:
                try:
                    file_repo(token, repo["node_id"], lists[target]["id"])
                    wrote = True
                    # Only a write that actually landed updates the in-memory
                    # view, which feeds the drift counts and the `keep_lists`
                    # rot exemption below. Adding unconditionally invented
                    # membership for repos nothing was even attempted for —
                    # inflating a list past `oversize`, or silencing a rot
                    # signal for a repo that is not in the exempt list at all.
                    lists[target]["items"].add(repo["full_name"])
                except ApiError as exc:
                    # Some repos are already filed at this point — those writes
                    # are committed and cannot be rolled back. Stop writing,
                    # but keep classifying so the report is still complete and
                    # still says what happened. Dying here would leave real
                    # writes with no record of them anywhere.
                    filing_error = str(exc)
                    print(f"star-curator: filing stopped: {exc}", file=sys.stderr)
                    may_file = False
            filed.append((repo["full_name"], target, wrote))
        else:
            ambiguous.append((repo, matched, present))

    rot = rot_signals(stars, rules, lists)

    oversize = int(rules.get("oversize", 30))
    drift = []
    for name, spec in sorted(lists.items()):
        count = len(spec["items"])
        if count == 0:
            drift.append(f"**{name}** is empty")
        elif count > oversize:
            drift.append(f"**{name}** holds {count} repos (over {oversize})")

    report = build_report(
        filed, ambiguous, rot, drift, args.dry_run, list_api_error, filing_error
    )
    print(report)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)

    # An API failure IS a finding. Without it a degraded run with nothing else
    # to say reported "nothing to report" and opened no issue — the quietest
    # possible outcome for the loudest possible problem.
    has_findings = bool(ambiguous or rot or drift or list_api_error or filing_error)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"has_findings={'true' if has_findings else 'false'}\n")
            fh.write(f"filed_count={len(filed)}\n")
    # Red check when the List API has moved or filing broke part-way, but only
    # after the report has been printed and written.
    return 1 if (list_api_error or filing_error) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as exc:
        # A traceback here is just noise in a job log — the useful content is
        # the method, URL and status, which ApiError already carries.
        raise SystemExit(f"star-curator: {exc}") from exc
