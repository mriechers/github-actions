#!/usr/bin/env python3
"""pr-watch review-state label helper — Python stdlib only.

Pure transition/verdict helpers (unit-tested) plus a thin `gh` I/O layer and a
CLI. No third-party dependencies.

This file is the canonical label taxonomy for the autonomous PR review system
(the unification spec in planning/ names it as the single definition). Every
other surface — ship_scan.py, review_publish.py, claude-review.yml, skill
prose — derives from or delegates to the constants below. Do not restate a
label name, color, or description anywhere else.

Label axes:
  - Axis R (`REVIEW_STATES`): exactly one per PR, enforced by plan_transition.
  - Axis S (`SHIP_STATES`): at most one per PR. Written with `gh pr edit`
    after `ensure` bootstraps the taxonomy — deliberately NOT via `set`,
    which exits 2 on a ship:* state (shipping disposition is not a review
    verdict; see the unification spec).
  - Flags (`FLAG_LABELS`): orthogonal, coexist freely with both axes.
"""

import argparse
import json
import subprocess
import sys
from typing import Callable, Iterable, List, Optional, Set, Tuple

# (name, hex color without '#', description)
TAXONOMY: List[Tuple[str, str, str]] = [
    ("review:new",          "ededed", "Awaiting first review"),
    ("review:re-review",    "1d76db", "Fixes pushed — awaiting re-review"),
    ("review:blocker",      "b60205", "Review done — must-fix changes requested"),
    ("review:nits",         "fbca04", "Review done — only minor/nit findings"),
    ("review:approved",     "0e8a16", "Review done — clean, no changes needed"),
    ("review:inconclusive", "d4c5f9", "Review attempted — no verdict could be established; bounded retry"),
    ("ship:ready",          "5319e7", "Approved + ship-pr confirms merge-ready"),
    ("ship:blocked",        "d93f0b", "A shipping agent triaged this and needs your decision"),
    ("ship:escalated",      "e99695", "Loop stopped — hard-stop, contested limit, budget, or no-progress; needs a human"),
    ("ship:parked",         "bfd4f2", "Redesign required — parked as draft with a resume note"),
    ("ship:deferred",       "c2e0c6", "Valid cross-repo or follow-up limitation documented — deferred"),
    ("ship:superseded",     "8b949e", "Closed in favor of another PR"),
    ("ship:probe",          "f9d0c4", "Disposable validation PR — must close, never merge"),
    ("claude-fix",          "c5def5", "Route to the /ship-pr loop"),
    ("no-pr-watch",         "cccccc", "Opt out of the local /pr-watch reviewer"),
    ("claude-review",       "1f6feb", "Escalate this PR to an agent for a deeper review pass"),
]

# Axis R — exactly one of these may be on a PR at a time.
REVIEW_STATES: Tuple[str, ...] = (
    "review:new",
    "review:re-review",
    "review:blocker",
    "review:nits",
    "review:approved",
    "review:inconclusive",
)

# Axis S — at most one of these may be on a PR at a time. ship:blocked is
# non-terminal (an agent triaged the PR and resumes on release); ship:ready is
# terminal-until-revoked (a new commit or blocker verdict revokes it); the
# rest are terminal dispositions a human queries and acts on.
SHIP_STATES: Tuple[str, ...] = (
    "ship:ready",
    "ship:blocked",
    "ship:escalated",
    "ship:parked",
    "ship:deferred",
    "ship:superseded",
    "ship:probe",
)

TERMINAL_SHIP_STATES: Tuple[str, ...] = (
    "ship:escalated",
    "ship:parked",
    "ship:deferred",
    "ship:superseded",
    "ship:probe",
)

# Everything a human must act on, in one query:
#   gh api -X GET search/issues \
#     -f q='is:pr is:open author:@me label:ship:ready,ship:blocked,ship:escalated'
USER_COURT_LABELS: Tuple[str, ...] = ("ship:ready", "ship:blocked", "ship:escalated")

# Orthogonal routing flags — coexist freely with both axes.
FLAG_LABELS: Tuple[str, ...] = ("claude-fix", "no-pr-watch", "claude-review")

BLOCKING_SEVERITIES = {"High", "Medium"}
MINOR_SEVERITIES = {"Low", "Nit"}

# review:blocker means "must fix"; ship:ready means "cleared to merge".
# They must never coexist, so a blocker verdict revokes the clearance.
REVOKES_SHIP_READY = "review:blocker"


class UnknownStateError(ValueError):
    """An unrecognized review state or finding severity was requested.

    A ValueError subclass so existing `except ValueError` callers still catch
    it, but distinct so `main()` can route it to exit 2 ("bad taxonomy input")
    while other ValueError subclasses (e.g. json.JSONDecodeError from a
    malformed `gh` response) fall through to the generic exit 1 ("real I/O
    failure") instead of being mislabeled as a taxonomy typo.
    """


def plan_transition(current_labels: Iterable[str],
                    target: str) -> Tuple[List[str], List[str]]:
    """Plan the label swap onto `target`.

    Returns (to_add, to_remove). Adds the target, removes every *other*
    review-state label plus any non-canonical `review:*` label (a hand-made
    `review:wip`, or a legacy name not yet migrated), and revokes ship:ready
    on a blocker verdict. Returns ([], []) when the PR is already in the
    desired state, so callers can run this unconditionally.
    """
    if target not in REVIEW_STATES:
        raise UnknownStateError(
            f"unknown review state: {target!r} (expected one of {REVIEW_STATES})")
    current = set(current_labels)
    to_add = [] if target in current else [target]
    to_remove = sorted(
        name for name in current
        if (name in REVIEW_STATES or name.startswith("review:")) and name != target)
    if target == REVOKES_SHIP_READY and "ship:ready" in current:
        to_remove.append("ship:ready")
    return to_add, to_remove


def verdict_from_findings(severities: Iterable[str]) -> str:
    """Map a round's finding severities to a verdict label. Worst wins."""
    seen = {s.strip().capitalize() for s in severities if s and s.strip()}
    unknown = seen - BLOCKING_SEVERITIES - MINOR_SEVERITIES
    if unknown:
        raise UnknownStateError(
            f"unknown severities: {sorted(unknown)} "
            f"(expected High/Medium/Low/Nit)")
    if seen & BLOCKING_SEVERITIES:
        return "review:blocker"
    if seen & MINOR_SEVERITIES:
        return "review:nits"
    return "review:approved"


def _gh(args: List[str]) -> str:
    """Run a gh command and return stdout."""
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return out.stdout


def _existing_label_names(repo: str,
                          run: Optional[Callable[[List[str]], str]] = None) -> Set[str]:
    """Names of labels already defined in the repo.

    Reads through `run` when supplied, so a caller injecting `run` to stub
    writes gets a stubbed read too instead of silently shelling out for
    real; falls back to the real `gh` otherwise — the path `ensure`'s dry
    run takes, since it has no runner to route a read through. Parse errors
    propagate rather than being swallowed: a `gh` response that doesn't
    parse is a real failure, not "this repo has no labels".
    """
    reader = run or _gh
    out = reader(["label", "list", "--repo", repo, "--limit", "200", "--json", "name"])
    return {row["name"] for row in json.loads(out)}


def _pr_labels(repo: str, pr: int) -> List[str]:
    """Label names currently applied to a PR."""
    out = _gh(["pr", "view", str(pr), "--repo", repo, "--json", "labels"])
    return [row["name"] for row in json.loads(out)["labels"]]


def ensure(repo: str,
          existing: Optional[Set[str]] = None,
          run: Optional[Callable[[List[str]], str]] = None,
          reconcile: bool = False,
          fetch_existing: Optional[Callable[[str], Set[str]]] = None) -> List[str]:
    """Idempotently create the taxonomy in `repo`. Returns names written (or,
    on a dry run, names that would be written).

    Costs one `gh label list` call and, in steady state, zero writes — so
    set_state() can call it before every label write without burning quota.
    `reconcile=True` force-upserts the whole taxonomy instead, repairing
    drifted colors and descriptions; that is the explicit `ensure --reconcile`
    path, not the per-review one.

    run=None is a dry run: existing labels are still consulted — via
    `fetch_existing` if supplied, else a real `gh label list` call — so the
    returned list reflects what would actually be written, not a hypothesis
    computed against an empty repo. Only the `gh label create` calls are
    skipped. Mirrors set_state()'s dry-run contract.
    """
    if existing is None:
        fetch = fetch_existing or (lambda r: _existing_label_names(r, run))
        existing = fetch(repo)
    written = []
    for name, color, desc in TAXONOMY:
        if name in existing and not reconcile:
            continue
        written.append(name)
        if run is not None:
            run(["label", "create", name, "--repo", repo,
                 "--color", color, "--description", desc, "--force"])
    return written


def set_state(repo: str, pr: int, state: str,
              fetch_labels: Optional[Callable[[str, int], List[str]]] = None,
              run: Optional[Callable[[List[str]], str]] = None
              ) -> Tuple[List[str], List[str]]:
    """Move `pr` to review-state `state`, bootstrapping the taxonomy first.

    Raises UnknownStateError (a ValueError subclass) on an unknown state
    before performing any write — and before the fetch — so a typo fails
    loudly instead of inventing an off-taxonomy label.

    run=None is a dry run: current labels are still read, so the returned plan
    is what would actually happen. Only mutations are skipped.
    Returns the applied (to_add, to_remove).
    """
    if state not in REVIEW_STATES:
        raise UnknownStateError(
            f"unknown review state: {state!r} (expected one of {REVIEW_STATES})")
    fetch = fetch_labels or _pr_labels
    current = fetch(repo, pr)
    to_add, to_remove = plan_transition(current, state)
    if not to_add and not to_remove:
        return [], []
    if run is not None:
        ensure(repo, run=run)
        args = ["pr", "edit", str(pr), "--repo", repo]
        for name in to_add:
            args += ["--add-label", name]
        for name in to_remove:
            args += ["--remove-label", name]
        run(args)
    return to_add, to_remove


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: set <owner/repo> <pr> (<state> | --from-severities S1,S2) [--dry-run]
    | ensure <owner/repo> [--dry-run]"""
    ap = argparse.ArgumentParser(description="pr-watch review-state labels")
    sub = ap.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="move a PR to a review state")
    p_set.add_argument("repo")
    p_set.add_argument("pr", type=int)
    state_group = p_set.add_mutually_exclusive_group(required=True)
    state_group.add_argument("state", nargs="?", default=None,
                             help="full review-state label name, e.g. review:blocker")
    state_group.add_argument("--from-severities",
                             help="comma-separated finding severities tagged this round "
                                  "(High/Medium/Low/Nit, empty string if none) — the "
                                  "state is computed via verdict_from_findings instead "
                                  "of chosen by the caller")
    p_set.add_argument("--dry-run", action="store_true")

    p_ensure = sub.add_parser("ensure", help="bootstrap the taxonomy in a repo")
    p_ensure.add_argument("repo")
    p_ensure.add_argument("--dry-run", action="store_true")
    p_ensure.add_argument("--reconcile", action="store_true",
                          help="force-upsert all labels, repairing drifted colors")

    args = ap.parse_args(argv)
    run = None if args.dry_run else _gh

    try:
        if args.command == "ensure":
            names = ensure(args.repo, run=run, reconcile=args.reconcile)
            print(f"{args.repo}: {len(names)} labels written"
                  f"{' (dry run)' if args.dry_run else ''}")
            return 0
        if args.from_severities is not None:
            state = verdict_from_findings(args.from_severities.split(","))
        else:
            state = args.state
        to_add, to_remove = set_state(args.repo, args.pr, state, run=run)
        print(f"{args.repo}#{args.pr}: +{to_add} -{to_remove}"
              f"{' (dry run)' if args.dry_run else ''}")
        return 0
    except UnknownStateError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"gh command failed (exit {exc.returncode}): {exc.stderr or ''}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
