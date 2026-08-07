#!/usr/bin/env python3
"""Deterministic publisher for the reusable Claude review workflow.

Extracted from claude-review.yml's inline python heredocs so the most
consequential logic in the workflow — verdict validation, diff-scoping,
retry eligibility, and the review/check payloads — is importable and
unit-tested instead of trapped in YAML.

Subcommands (each reads env vars the workflow step provides):

    validate     Validate Claude's structured output, demote out-of-diff
                 blocking findings, and write claude-review-publication.json
                 plus $GITHUB_OUTPUT entries.
    compose      Compose claude-review-request.json (formal review, with the
                 pr-review:v1 record embedded in the body) and
                 claude-review-check.json from the publication file.
    eligibility  Decide whether to publish for this head: skip when a
                 conclusive completed check exists; allow bounded retries
                 after inconclusive ones.

The verdict vocabulary is the workflow's structured-output enum; the label
vocabulary is pr_label.TAXONOMY. This module is the only place the two are
joined, and the join is asserted against pr_label at import time so the
definitions cannot drift apart silently.
"""

import json
import os
import sys
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

_SKILL_SCRIPTS = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir,
    ".claude", "skills", "pr-watch", "scripts")
sys.path.insert(0, _SKILL_SCRIPTS)

import pr_label  # noqa: E402
import pr_record  # noqa: E402

CHECK_NAME = "Claude autonomous review"
SUMMARY_LIMIT = 60000

# verdict -> (review:* label, check conclusion, record outcome)
VERDICTS: Dict[str, Tuple[str, str, str]] = {
    "approve":         ("review:approved",     "success", "approved"),
    "request_changes": ("review:blocker",      "failure", "blocker"),
    "comment":         ("review:nits",         "neutral", "nits"),
    "inconclusive":    ("review:inconclusive", "neutral", "inconclusive"),
}

# Only a formal review from an App identity may APPROVE; without one a clean
# verdict downgrades to COMMENT while the label and check still record it.
CLEAN_APPROVAL_NOTE = (
    "Clean verdict: formal approval was not submitted because GitHub App "
    "credentials are not configured; review:approved and this successful "
    "check record the result.")

assert all(label in pr_label.REVIEW_STATES for label, _, _ in VERDICTS.values()), \
    "review_publish verdict labels must be pr_label REVIEW_STATES"
assert all(outcome in pr_record.OUTCOMES for _, _, outcome in VERDICTS.values()), \
    "review_publish outcomes must be pr_record OUTCOMES"


class PublishError(Exception):
    """A validation failure that must fail the workflow step."""


def parse_structured_output(raw: str) -> Dict:
    """Validate Claude's structured output. Raises PublishError on any
    deviation — the schema is re-checked here because the action's own
    validation is not this workflow's to rely on."""
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PublishError(f"Claude did not produce valid structured output: {error}")
    if not isinstance(result, dict):
        raise PublishError("Claude structured output must be a JSON object")
    allowed = {"verdict", "summary", "findings"}
    if not set(result) <= allowed or not {"verdict", "summary"} <= set(result):
        raise PublishError(
            "Claude structured output must contain verdict and summary "
            "(findings optional) and nothing else")
    if result["verdict"] not in VERDICTS:
        raise PublishError(
            "Claude structured output verdict must be one of "
            + ", ".join(sorted(VERDICTS)))
    summary = result["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise PublishError("Claude structured output summary must be a non-empty string")
    if len(summary.strip()) > SUMMARY_LIMIT:
        raise PublishError(f"Claude review summary exceeds {SUMMARY_LIMIT} characters")
    findings = result.get("findings", [])
    if not isinstance(findings, list):
        raise PublishError("Claude structured output findings must be a list")
    for finding in findings:
        if (not isinstance(finding, dict)
                or not {"id", "severity", "path", "summary"} <= set(finding)):
            raise PublishError(
                "each finding needs id, severity, path, and summary")
    return {"verdict": result["verdict"], "summary": summary.strip(),
            "findings": findings}


def apply_diff_scoping(verdict: str, findings: Sequence[Dict],
                       changed_paths: Sequence[str]
                       ) -> Tuple[str, List[Dict], List[Dict]]:
    """Demote out-of-diff blocking findings and re-derive the verdict.

    A path outside the PR's changed-file set cannot be a blocker (the
    empty-tree-diff false-blocker fix, applied mechanically). When every
    blocking finding was out of scope, a request_changes verdict downgrades
    to comment; the demoted findings are reported as non-blocking follow-ups.
    An empty changed-file set means the diff itself was unobtainable — that
    is an inconclusive review, not a license to block on the whole repo.
    """
    scoped = [dict(f, status=f.get("status", "open")) for f in findings]
    if verdict not in ("request_changes", "comment") or not scoped:
        return verdict, scoped, []
    if not changed_paths:
        return "inconclusive", [], scoped
    in_scope, demoted = pr_record.validate_blocking_paths(scoped, changed_paths)
    if verdict == "request_changes":
        severities = [f["severity"] for f in in_scope if f.get("severity")]
        try:
            derived = pr_label.verdict_from_findings(severities)
        except pr_label.UnknownStateError:
            derived = "review:blocker"  # unknown severity: keep the block
        if derived != "review:blocker":
            verdict = "comment"
    return verdict, in_scope, demoted


def cmd_validate() -> int:
    raw = os.environ["STRUCTURED_OUTPUT"]
    changed = [p for p in os.environ.get("CHANGED_FILES", "").splitlines()
               if p.strip()]
    result = parse_structured_output(raw)
    verdict, in_scope, demoted = apply_diff_scoping(
        result["verdict"], result["findings"], changed)

    summary = result["summary"]
    if demoted:
        lines = "\n".join(
            f"- `{f['path']}` — {f['summary']} "
            f"(severity {f['severity']}; outside this PR's diff)"
            for f in demoted)
        summary += (
            "\n\n### Out-of-scope observations (non-blocking follow-ups)\n"
            "The following cite paths outside this PR's changed files and "
            "cannot block it:\n" + lines)
    if verdict == "inconclusive" and result["verdict"] != "inconclusive":
        summary += (
            "\n\nNo diff content was available to scope findings against; "
            "recorded as inconclusive rather than blocking on out-of-diff "
            "observations.")

    label, conclusion, outcome = VERDICTS[verdict]
    publication = {
        "verdict": verdict,
        "event": {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES",
                  "comment": "COMMENT", "inconclusive": "COMMENT"}[verdict],
        "label": label,
        "summary": summary,
        "check_conclusion": conclusion,
        "outcome": outcome,
        "findings": in_scope,
        "demoted_findings": demoted,
    }
    with open("claude-review-publication.json", "w", encoding="utf-8") as file:
        json.dump(publication, file)

    delimiter = f"review-{uuid.uuid4()}"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"verdict={verdict}\n")
        output.write(f"label={label}\n")
        output.write(f"summary<<{delimiter}\n{summary}\n{delimiter}\n")
    return 0


def build_review_record(publication: Dict, pr_number: str, head_sha: str,
                        run_id: str, requested_at: str) -> Dict:
    """The pr-review:v1 record this review contributes, embedded in the formal
    review body so prose and record land atomically in one API call."""
    findings = [
        {"id": f["id"], "severity": f["severity"], "path": f["path"],
         "summary": f["summary"], "status": f.get("status", "open")}
        for f in publication["findings"]
    ]
    return {
        "kind": "review",
        "id": f"rvw-gha-{run_id}",
        "request_id": f"req-gha-{run_id}",
        "role": "primary",
        "reviewer": "claude-review-action",
        "dispatched_sha": head_sha,
        "reviewed_sha": head_sha,
        "outcome": publication["outcome"],
        "findings": findings,
        "reviewed_at": requested_at,
    }


def build_request_record(pr_number: str, head_sha: str, run_id: str,
                         requested_at: str) -> Dict:
    """The Action synthesizes its own request when none exists: it is the
    primary reviewer and its trigger is the head change that started the run."""
    return {
        "kind": "request",
        "id": f"req-gha-{run_id}",
        "trigger": "head-change",
        "head_sha": head_sha,
        "requested_at": requested_at,
        "selected_role": "primary",
    }


def cmd_compose() -> int:
    with open("claude-review-publication.json", encoding="utf-8") as file:
        publication = json.load(file)

    has_app_token = os.environ.get("HAS_APP_TOKEN") == "true"
    head_sha = os.environ["PR_HEAD_SHA"]
    pr_number = os.environ["PR_NUMBER"]
    run_id = os.environ.get("GH_RUN_ID", "0")
    requested_at = os.environ.get(
        "REVIEWED_AT", "1970-01-01T00:00:00Z")

    event = publication["event"]
    body = publication["summary"]
    if publication["verdict"] == "approve" and not has_app_token:
        event = "COMMENT"
        body = f"{body}\n\n{CLEAN_APPROVAL_NOTE}"

    request_record = build_request_record(
        pr_number, head_sha, run_id, requested_at)
    review_record = build_review_record(
        publication, pr_number, head_sha, run_id, requested_at)
    body += ("\n\n" + pr_record.serialize(request_record)
             + "\n" + pr_record.serialize(review_record))

    with open("claude-review-request.json", "w", encoding="utf-8") as file:
        json.dump({"body": body, "event": event}, file)

    with open("claude-review-check.json", "w", encoding="utf-8") as file:
        json.dump(
            {
                "name": CHECK_NAME,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": publication["check_conclusion"],
                "output": {
                    "title": f"Review: {publication['verdict']}",
                    "summary": publication["summary"][:60000],
                },
            },
            file,
        )
    return 0


def decide_eligibility(check_runs: Sequence[Dict],
                       bound: int = pr_record.INCONCLUSIVE_RETRY_BOUND
                       ) -> Tuple[bool, str]:
    """Publish for this head unless a conclusive completed review check
    already exists. Inconclusive checks (title `Review: inconclusive`) allow
    bounded retries — a permanently suppressed retry would make one blocked
    sandbox the review of record forever."""
    ours = [c for c in check_runs
            if c.get("name") == CHECK_NAME
            and (c.get("status") or "").lower() == "completed"]
    inconclusive = [
        c for c in ours
        if ((c.get("output") or {}).get("title") or "") == "Review: inconclusive"]
    conclusive = [c for c in ours if c not in inconclusive]
    if conclusive:
        return False, "a conclusive completed review check already exists for this head"
    if len(inconclusive) >= bound:
        return False, (f"retry budget exhausted: {len(inconclusive)} "
                       f"inconclusive attempts (bound {bound})")
    if inconclusive:
        return True, (f"retrying after {len(inconclusive)} inconclusive "
                      f"attempt(s) (bound {bound})")
    return True, "no completed review check for this head"


def cmd_eligibility() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    check_runs = payload.get("check_runs", [])
    publish, reason = decide_eligibility(check_runs)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"publish={'true' if publish else 'false'}\n")
        output.write(f"reason={reason}\n")
    print(reason)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"validate": cmd_validate, "compose": cmd_compose,
                "eligibility": cmd_eligibility}
    if len(argv) != 1 or argv[0] not in commands:
        sys.stderr.write(
            f"usage: review_publish.py ({' | '.join(sorted(commands))})\n")
        return 2
    try:
        return commands[argv[0]]()
    except PublishError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except KeyError as exc:
        sys.stderr.write(f"missing required environment variable: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
