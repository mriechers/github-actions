#!/usr/bin/env python3
"""pr-review:v1 protocol records — Python stdlib only.

Versioned, machine-readable records embedded in PR comments are the durable
evidence for review requests, reviews, finding dispositions, and terminal
state. Labels stay the cheap query index; these records are the source of
truth for who reviewed what, why, and the current open-finding set. Local
state files are execution caches only — every counter here is rebuildable
from the PR thread alone.

Marker form (compact JSON inside an HTML comment, invisible in rendered
prose):

    <!-- pr-review:v1 {"kind":"review","id":"rvw-...","request_id":"req-...",
    "role":"fallback","reviewer":"pr-watch","dispatched_sha":"...",
    "reviewed_sha":"...","outcome":"approved","findings":[...],
    "reviewed_at":"2026-08-06T12:00:00Z"} -->

Required fields per kind:

    request     id, trigger (head-change|mention|manual|fallback), head_sha,
                requested_at, selected_role (primary|fallback)
    review      id, request_id, role, reviewer, dispatched_sha, reviewed_sha,
                outcome (approved|nits|blocker|inconclusive), findings,
                reviewed_at        [optional: ci_state, base_state]
    disposition id, finding_id, status, evidence, recorded_at
    terminal    id, state (ready|escalated|parked|deferred|superseded|probe),
                reason, recorded_at

Findings are {id, severity, path, summary, status} with reviewer-assigned
stable slug ids (e.g. "pr:551:ci-failure") that never change across rounds.
Roles: primary (the Action), fallback (/pr-watch), advisory (Copilot et al.
— can never satisfy a request or advance progress), author (disposition
writer — makes author-side records self-identifying even when the GitHub
login is identical to the reviewer's).

Malformed, unsupported-version, or duplicate-id records are ignored for
state selection and surfaced as protocol errors — never a crash, never
silently trusted.
"""

import json
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

PROTOCOL_VERSION = "pr-review:v1"

MARKER_RE = re.compile(
    r"<!--\s*pr-review:(?P<version>v\d+)\s+(?P<json>\{.*?\})\s*-->",
    re.DOTALL,
)

# Legacy pr-watch dedup marker, readable during the migration window but
# never emitted by updated writers.
LEGACY_MARKER_RE = re.compile(r"<!--\s*pr-watch:\s*sha=([0-9a-fA-F]{7,40})\s*-->")

TRIGGERS = ("head-change", "mention", "manual", "fallback")
ROLES = ("primary", "fallback", "advisory", "author")
VERDICT_ROLES = ("primary", "fallback")  # roles whose reviews can carry a verdict
OUTCOMES = ("approved", "nits", "blocker", "inconclusive")
FINDING_STATUSES = ("open", "fixed", "contested", "deferred", "superseded", "redesign")
TERMINAL_STATES = ("ready", "escalated", "parked", "deferred", "superseded", "probe")
CI_STATES = ("green", "failing-attributed", "failing-unattributed",
             "unavailable", "absent")

# An inconclusive review may be retried this many times total per request
# (initial attempt + retries). Past the bound, the request escalates to the
# human report instead of looping.
INCONCLUSIVE_RETRY_BOUND = 3

REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "request": ("id", "trigger", "head_sha", "requested_at", "selected_role"),
    "review": ("id", "request_id", "role", "reviewer", "dispatched_sha",
               "reviewed_sha", "outcome", "findings", "reviewed_at"),
    "disposition": ("id", "finding_id", "status", "evidence", "recorded_at"),
    "terminal": ("id", "state", "reason", "recorded_at"),
}

_TS_FIELD = {
    "request": "requested_at",
    "review": "reviewed_at",
    "disposition": "recorded_at",
    "terminal": "recorded_at",
}


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; None on anything unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_time(record: Dict) -> datetime:
    """The record's own timestamp, for chronological ordering.

    Unparseable timestamps sort to the epoch — a malformed record must never
    win a most-recent race against a well-formed one.
    """
    ts = _parse_ts(record.get(_TS_FIELD.get(record.get("kind"), ""), ""))
    return ts or datetime.fromtimestamp(0, tz=timezone.utc)


def _validate(record: Dict) -> Optional[str]:
    """Return an error string, or None when the record is well-formed."""
    kind = record.get("kind")
    if kind not in REQUIRED_FIELDS:
        return f"unknown kind: {kind!r}"
    missing = [f for f in REQUIRED_FIELDS[kind] if f not in record]
    if missing:
        return f"{kind} record {record.get('id', '?')!r} missing {missing}"
    if kind == "request":
        if record["trigger"] not in TRIGGERS:
            return f"request {record['id']!r}: unknown trigger {record['trigger']!r}"
        if record["selected_role"] not in VERDICT_ROLES:
            return (f"request {record['id']!r}: selected_role must be one of "
                    f"{VERDICT_ROLES}, got {record['selected_role']!r}")
    if kind == "review":
        if record["role"] not in ROLES:
            return f"review {record['id']!r}: unknown role {record['role']!r}"
        if record["outcome"] not in OUTCOMES:
            return f"review {record['id']!r}: unknown outcome {record['outcome']!r}"
        if not isinstance(record["findings"], list):
            return f"review {record['id']!r}: findings must be a list"
        for finding in record["findings"]:
            if not isinstance(finding, dict) or "id" not in finding:
                return f"review {record['id']!r}: finding without a stable id"
            if finding.get("status") not in FINDING_STATUSES:
                return (f"review {record['id']!r}: finding {finding['id']!r} "
                        f"has unknown status {finding.get('status')!r}")
        if "ci_state" in record and record["ci_state"] not in CI_STATES:
            return f"review {record['id']!r}: unknown ci_state {record['ci_state']!r}"
    if kind == "disposition":
        if record["status"] not in FINDING_STATUSES:
            return (f"disposition {record['id']!r}: unknown status "
                    f"{record['status']!r}")
        if not str(record["evidence"]).strip():
            return f"disposition {record['id']!r}: evidence must be non-empty"
    if kind == "terminal":
        if record["state"] not in TERMINAL_STATES:
            return f"terminal {record['id']!r}: unknown state {record['state']!r}"
    return None


def parse_records(bodies: Iterable[str]) -> Tuple[List[Dict], List[str]]:
    """Extract well-formed records from comment bodies, in chronological order.

    Returns (records, errors). Malformed JSON, unsupported versions,
    validation failures, and duplicate ids all land in `errors` and never in
    `records` — protocol errors are reported, not interpolated into state.
    """
    records: List[Dict] = []
    errors: List[str] = []
    seen_ids: Set[str] = set()
    for body in bodies:
        if not body:
            continue
        for match in MARKER_RE.finditer(body):
            if match.group("version") != "v1":
                errors.append(f"unsupported version: {match.group('version')}")
                continue
            try:
                record = json.loads(match.group("json"))
            except json.JSONDecodeError as exc:
                errors.append(f"malformed record JSON: {exc}")
                continue
            problem = _validate(record)
            if problem:
                errors.append(problem)
                continue
            if record["id"] in seen_ids:
                errors.append(f"duplicate record id: {record['id']!r}")
                continue
            seen_ids.add(record["id"])
            records.append(record)
    records.sort(key=record_time)
    return records, errors


def serialize(record: Dict) -> str:
    """Render a record as its comment marker. Refuses malformed records."""
    problem = _validate(record)
    if problem:
        raise ValueError(problem)
    payload = json.dumps(record, separators=(",", ":"), sort_keys=True)
    return f"<!-- {PROTOCOL_VERSION} {payload} -->"


def legacy_reviewed_shas(bodies: Iterable[str]) -> List[str]:
    """SHAs from legacy `pr-watch: sha=` markers, oldest first.

    Migration-window read path only; updated writers emit v1 records.
    """
    shas: List[str] = []
    for body in bodies:
        if body:
            shas.extend(m.group(1) for m in LEGACY_MARKER_RE.finditer(body))
    return shas


def of_kind(records: Sequence[Dict], kind: str) -> List[Dict]:
    return [r for r in records if r.get("kind") == kind]


def latest_request(records: Sequence[Dict]) -> Optional[Dict]:
    """The current review request — the most recently issued one."""
    requests = of_kind(records, "request")
    return requests[-1] if requests else None


def dedupe_request(records: Sequence[Dict], request_id: str) -> bool:
    """True when `request_id` already exists — dedup key is the request id,
    never the SHA, so a mention-triggered re-review of an unchanged head is a
    new request while a re-delivered webhook for the same request is not."""
    return any(r["id"] == request_id for r in of_kind(records, "request"))


def current_review(records: Sequence[Dict], head_sha: str) -> Optional[Dict]:
    """The review that currently speaks for the PR, or None.

    A review is current when: it answers the current request, it was written
    by the request's selected role (the sole verdict writer — anything else
    is advisory evidence), and its `reviewed_sha` equals the live head.
    `reviewed_sha != dispatched_sha` is expected and valid: workers re-resolve
    the head at start, and a leader must never treat that drift as
    "didn't review".
    """
    request = latest_request(records)
    if request is None:
        return None
    candidates = [
        r for r in of_kind(records, "review")
        if r["request_id"] == request["id"]
        and r["role"] == request["selected_role"]
        and r["reviewed_sha"] == head_sha
    ]
    return candidates[-1] if candidates else None


def verdict_for(records: Sequence[Dict], head_sha: str) -> Optional[str]:
    """The current verdict outcome, or None when no current selected review
    exists. Advisory reviews can never produce a verdict, satisfy a request,
    or reset progress — a null reviewer must not make "a review landed"
    true forever."""
    review = current_review(records, head_sha)
    return review["outcome"] if review else None


def open_findings(records: Sequence[Dict]) -> Dict[str, Dict]:
    """The open-finding set as {finding_id: finding}, by set difference.

    Chronological walk: a verdict-role review listing a finding as `open`
    (re-)opens it; a finding leaves the open set only through a disposition
    record (which carries evidence — enforced at parse time). A later review
    listing a dispositioned id as `open` again re-opens it: findings do not
    decrease monotonically, and fixes generate findings.

    Advisory reviews never contribute findings — they are input a real
    reviewer may adopt, not state.
    """
    latest: Dict[str, Tuple[datetime, str, Dict]] = {}
    for record in records:
        when = record_time(record)
        if record.get("kind") == "review" and record.get("role") in VERDICT_ROLES:
            for finding in record["findings"]:
                if finding["status"] == "open":
                    latest[finding["id"]] = (when, "open", finding)
        elif record.get("kind") == "disposition":
            fid = record["finding_id"]
            prior = latest.get(fid)
            detail = prior[2] if prior else {"id": fid}
            latest[fid] = (when, record["status"], detail)
    return {fid: detail for fid, (_, status, detail) in latest.items()
            if status == "open"}


def can_retry(records: Sequence[Dict], request_id: str,
              bound: int = INCONCLUSIVE_RETRY_BOUND) -> bool:
    """Whether another attempt at `request_id` is allowed.

    Inconclusive attempts are bounded per request; they neither increment the
    author round counter nor reset the no-progress timer, so the only brake
    on an always-inconclusive reviewer is this bound.
    """
    attempts = [
        r for r in of_kind(records, "review")
        if r["request_id"] == request_id and r["outcome"] == "inconclusive"
    ]
    return len(attempts) < bound


def terminal_state(records: Sequence[Dict]) -> Optional[Dict]:
    """The PR's terminal record, or None. Most recent wins (a probe close
    after an escalation is a deliberate correction, not a conflict)."""
    terminals = of_kind(records, "terminal")
    return terminals[-1] if terminals else None


def classify_ci(check_rollup: Optional[Sequence[Dict]],
                attributed_failures: Optional[Set[str]] = None) -> str:
    """Classify CI evidence. An empty check list is `absent`, never green —
    `all([])` is True and 4 of 9 actionable repos have no CI at all.

    `check_rollup` entries need `name`, `status`, and `conclusion` (the
    shape of `statusCheckRollup`). None means the rollup could not be read
    at all. `attributed_failures` is the set of failing check names backed
    by a per-failure reproduction/attribution record.
    """
    if check_rollup is None:
        return "unavailable"
    checks = list(check_rollup)
    if not checks:
        return "absent"
    if any((c.get("status") or "").upper() not in ("COMPLETED", "")
           and (c.get("status") or "").lower() != "completed"
           for c in checks):
        return "unavailable"
    failing = [
        c for c in checks
        if (c.get("conclusion") or "").lower() not in
        ("success", "neutral", "skipped")
    ]
    if not failing:
        return "green"
    attributed = attributed_failures or set()
    if all(c.get("name") in attributed for c in failing):
        return "failing-attributed"
    return "failing-unattributed"


def ship_ready_eligible(records: Sequence[Dict], head_sha: str,
                        ci_state: str, base_state: str) -> Tuple[bool, List[str]]:
    """The ship:ready gate. Requires ALL of: a current selected `approved`
    review with no open findings; CI green or failing-attributed; a fresh,
    conflict-free base; and no terminal record. Separate axes — never an
    optimistic reading of any one.

    Returns (eligible, reasons_blocking).
    """
    reasons: List[str] = []
    verdict = verdict_for(records, head_sha)
    if verdict != "approved":
        reasons.append(f"no current selected approved review (verdict: {verdict})")
    remaining = open_findings(records)
    if remaining:
        reasons.append(f"open findings: {sorted(remaining)}")
    if ci_state not in ("green", "failing-attributed"):
        reasons.append(f"ci_state is {ci_state}")
    if base_state != "fresh":
        reasons.append(f"base_state is {base_state}")
    terminal = terminal_state(records)
    if terminal is not None and terminal["state"] != "ready":
        reasons.append(f"terminal record present: {terminal['state']}")
    return (not reasons, reasons)


def validate_blocking_paths(findings: Sequence[Dict],
                            changed_paths: Iterable[str]
                            ) -> Tuple[List[Dict], List[Dict]]:
    """Split findings into (in_scope, demoted).

    A blocking (High/Medium) finding must cite a path in the PR's changed-file
    set. A path outside that set is an out-of-scope observation: it may
    create a follow-up but cannot block the PR — the mechanical fix for the
    empty-tree-diff false blocker, applied to every reviewer, not just the
    well-behaved ones. Non-blocking findings pass through regardless of path.
    """
    changed = set(changed_paths)
    in_scope: List[Dict] = []
    demoted: List[Dict] = []
    for finding in findings:
        blocking = str(finding.get("severity", "")).capitalize() in ("High", "Medium")
        if blocking and finding.get("path") not in changed:
            demoted.append(finding)
        else:
            in_scope.append(finding)
    return in_scope, demoted


def effective_severities(findings: Sequence[Dict],
                         changed_paths: Iterable[str]) -> List[str]:
    """Severities that may influence the verdict: out-of-diff blocking
    findings are excluded entirely (they become non-blocking follow-ups,
    not nits — an out-of-scope observation must not color the verdict)."""
    in_scope, _ = validate_blocking_paths(findings, changed_paths)
    return [f["severity"] for f in in_scope if f.get("severity")]


def no_progress(records: Sequence[Dict], now: datetime,
                timeout_seconds: float) -> bool:
    """True when nothing that counts as progress has happened within the
    timeout. Inconclusive reviews and advisory reviews do not reset the
    timer; requests, verdict-role conclusive reviews, dispositions, and
    terminals do."""
    latest: Optional[datetime] = None
    for record in records:
        kind = record.get("kind")
        if kind == "review":
            if record.get("role") not in VERDICT_ROLES:
                continue
            if record.get("outcome") == "inconclusive":
                continue
        when = record_time(record)
        if latest is None or when > latest:
            latest = when
    if latest is None:
        return False  # nothing has started; the adoption gate owns this case
    return (now - latest).total_seconds() > timeout_seconds


def rebuild_counters(records: Sequence[Dict]) -> Dict[str, int]:
    """Reconstruct loop counters from the PR thread alone.

    Local state files are caches; after a context loss this is the recovery
    path. `rounds` counts conclusive verdict-role reviews (inconclusive does
    not increment). `pushback_only_rounds` counts the consecutive most-recent
    review rounds whose author dispositions were all `contested` — the
    don't-loop-forever-on-contested-nits brake.
    """
    reviews = [
        r for r in of_kind(records, "review")
        if r["role"] in VERDICT_ROLES and r["outcome"] != "inconclusive"
    ]
    dispositions = of_kind(records, "disposition")
    rounds = len(reviews)

    pushback_only = 0
    boundaries = [record_time(r) for r in reviews] + [
        datetime.max.replace(tzinfo=timezone.utc)]
    for i in range(len(reviews) - 1, -1, -1):
        window = [
            d for d in dispositions
            if boundaries[i] <= record_time(d) < boundaries[i + 1]
        ]
        if window and all(d["status"] == "contested" for d in window):
            pushback_only += 1
        else:
            break
    return {"rounds": rounds, "pushback_only_rounds": pushback_only}
