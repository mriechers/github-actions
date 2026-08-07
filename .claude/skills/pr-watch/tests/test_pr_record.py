import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pr_record  # noqa: E402
from pr_record import (  # noqa: E402
    parse_records,
    serialize,
    legacy_reviewed_shas,
    latest_request,
    dedupe_request,
    current_review,
    verdict_for,
    open_findings,
    can_retry,
    terminal_state,
    classify_ci,
    ship_ready_eligible,
    validate_blocking_paths,
    effective_severities,
    no_progress,
    rebuild_counters,
)


def ts(hour, minute=0):
    return f"2026-08-06T{hour:02d}:{minute:02d}:00Z"


def req(rid="req-1", trigger="head-change", head="a" * 40,
        role="fallback", at=ts(9)):
    return {"kind": "request", "id": rid, "trigger": trigger, "head_sha": head,
            "requested_at": at, "selected_role": role}


def rvw(rid="rvw-1", request_id="req-1", role="fallback", outcome="approved",
        reviewed=None, dispatched=None, findings=(), at=ts(10), **extra):
    record = {"kind": "review", "id": rid, "request_id": request_id,
              "role": role, "reviewer": "pr-watch",
              "dispatched_sha": dispatched or "a" * 40,
              "reviewed_sha": reviewed or "a" * 40,
              "outcome": outcome, "findings": list(findings),
              "reviewed_at": at}
    record.update(extra)
    return record


def fnd(fid, severity="High", path="src/app.py", status="open"):
    return {"id": fid, "severity": severity, "path": path,
            "summary": "a finding", "status": status}


def dsp(did, finding_id, status="fixed", at=ts(11)):
    return {"kind": "disposition", "id": did, "finding_id": finding_id,
            "status": status, "evidence": "commit abc123 fixes it",
            "recorded_at": at}


def trm(tid="trm-1", state="superseded", at=ts(12)):
    return {"kind": "terminal", "id": tid, "state": state,
            "reason": "replaced by #999", "recorded_at": at}


def bodies(*records):
    return [serialize(r) for r in records]


class TestParse(unittest.TestCase):
    def test_round_trips_a_record(self):
        records, errors = parse_records(bodies(req()))
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "request")

    def test_marker_survives_surrounding_prose(self):
        body = "## pr-watch review\n\nProse here.\n\n" + serialize(req()) + "\nMore."
        records, errors = parse_records([body])
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)

    def test_malformed_json_is_an_error_not_state(self):
        records, errors = parse_records(['<!-- pr-review:v1 {"kind": -->'])
        self.assertEqual(records, [])
        # Truncated JSON never matches a closing brace cleanly; either the
        # regex skips it (no records) or json fails (error). Both are safe.

    def test_unsupported_version_is_an_error(self):
        records, errors = parse_records(['<!-- pr-review:v2 {"kind":"x"} -->'])
        self.assertEqual(records, [])
        self.assertTrue(any("unsupported version" in e for e in errors))

    def test_duplicate_id_ignored_for_state(self):
        first = req(at=ts(9))
        dupe = req(at=ts(10))
        records, errors = parse_records(bodies(first, dupe))
        self.assertEqual(len(records), 1)
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_missing_field_is_an_error(self):
        broken = req()
        del broken["head_sha"]
        body = ('<!-- pr-review:v1 ' + __import__("json").dumps(broken) + ' -->')
        records, errors = parse_records([body])
        self.assertEqual(records, [])
        self.assertTrue(any("missing" in e for e in errors))

    def test_serialize_refuses_malformed(self):
        with self.assertRaises(ValueError):
            serialize({"kind": "terminal", "id": "t", "state": "nope",
                       "reason": "r", "recorded_at": ts(9)})

    def test_disposition_requires_evidence(self):
        d = dsp("d-1", "f-1")
        d["evidence"] = "  "
        with self.assertRaises(ValueError):
            serialize(d)

    def test_records_sorted_chronologically_across_comments(self):
        records, _ = parse_records(bodies(rvw(at=ts(10)), req(at=ts(9))))
        self.assertEqual([r["kind"] for r in records], ["request", "review"])

    def test_legacy_markers_still_readable(self):
        shas = legacy_reviewed_shas(["<!-- pr-watch: sha=" + "b" * 40 + " -->"])
        self.assertEqual(shas, ["b" * 40])


class TestVerdictSelection(unittest.TestCase):
    def test_current_review_requires_head_match(self):
        records, _ = parse_records(bodies(req(head="a" * 40),
                                          rvw(reviewed="b" * 40)))
        self.assertIsNone(current_review(records, "a" * 40))

    def test_reviewed_sha_may_differ_from_dispatched(self):
        # Worker re-resolved the head mid-flight (#549 round 2, live): the
        # review is current because reviewed_sha equals the live head, even
        # though the dispatch SHA is stale. A leader treating this as
        # "didn't review" respawns forever.
        records, _ = parse_records(bodies(
            req(head="c" * 40),
            rvw(dispatched="a" * 40, reviewed="c" * 40)))
        self.assertIsNotNone(current_review(records, "c" * 40))
        self.assertEqual(verdict_for(records, "c" * 40), "approved")

    def test_advisory_review_never_satisfies(self):
        # Copilot is a null reviewer: it must never satisfy a progress
        # condition, or "a review landed since my push" is true forever.
        records, _ = parse_records(bodies(
            req(role="primary"),
            rvw(role="advisory", outcome="approved")))
        self.assertIsNone(verdict_for(records, "a" * 40))

    def test_non_selected_role_is_evidence_not_verdict(self):
        # The request selected the primary; a fallback review that raced it
        # remains evidence but cannot replace the selected verdict.
        records, _ = parse_records(bodies(
            req(role="primary"),
            rvw(role="fallback", outcome="blocker")))
        self.assertIsNone(verdict_for(records, "a" * 40))

    def test_selected_role_wins(self):
        records, _ = parse_records(bodies(
            req(role="primary"),
            rvw(rid="rvw-adv", role="advisory", outcome="approved", at=ts(10)),
            rvw(rid="rvw-pri", role="primary", outcome="nits", at=ts(11),
                reviewer="claude-review-action")))
        self.assertEqual(verdict_for(records, "a" * 40), "nits")

    def test_author_role_is_distinguishable_from_reviewer(self):
        # #511 regression: the same GitHub login wrote both an author note
        # and a genuine independent review, distinguishable only by prose.
        # With role in the record, the author's note can never be mistaken
        # for a review that satisfies the request.
        records, _ = parse_records(bodies(
            req(role="fallback"),
            rvw(rid="rvw-author", role="author", outcome="approved", at=ts(10)),
            rvw(rid="rvw-real", role="fallback", outcome="approved", at=ts(11))))
        current = current_review(records, "a" * 40)
        self.assertEqual(current["id"], "rvw-real")

    def test_request_dedup_is_by_id_not_sha(self):
        # #511: a mention-triggered re-review of an unchanged head is a NEW
        # request; SHA-keyed dedup suppressed it for four days.
        records, _ = parse_records(bodies(req(rid="req-1")))
        self.assertTrue(dedupe_request(records, "req-1"))
        self.assertFalse(dedupe_request(records, "req-2-mention"))

    def test_latest_request_wins(self):
        records, _ = parse_records(bodies(
            req(rid="req-1", at=ts(9)),
            req(rid="req-2", at=ts(10), role="primary")))
        self.assertEqual(latest_request(records)["id"], "req-2")


class TestOpenFindings(unittest.TestCase):
    def test_open_set_is_a_set_difference(self):
        records, _ = parse_records(bodies(
            req(),
            rvw(outcome="blocker",
                findings=[fnd("pr:7:a"), fnd("pr:7:b", severity="Low")]),
            dsp("d-1", "pr:7:a", status="fixed")))
        self.assertEqual(sorted(open_findings(records)), ["pr:7:b"])

    def test_finding_leaves_only_via_disposition(self):
        records, _ = parse_records(bodies(
            req(), rvw(outcome="blocker", findings=[fnd("pr:7:a")])))
        self.assertIn("pr:7:a", open_findings(records))

    def test_fixes_generate_findings(self):
        # Findings do not decrease monotonically (#484, #549 round 2): a
        # second round may re-open a dispositioned finding or add new ones.
        records, _ = parse_records(bodies(
            req(rid="req-1", at=ts(9)),
            rvw(rid="rvw-1", outcome="blocker", findings=[fnd("pr:7:a")],
                at=ts(10)),
            dsp("d-1", "pr:7:a", status="fixed", at=ts(11)),
            req(rid="req-2", at=ts(12)),
            rvw(rid="rvw-2", request_id="req-2", outcome="blocker",
                findings=[fnd("pr:7:a"), fnd("pr:7:c")], at=ts(13))))
        self.assertEqual(sorted(open_findings(records)), ["pr:7:a", "pr:7:c"])

    def test_advisory_findings_never_enter_the_open_set(self):
        records, _ = parse_records(bodies(
            req(), rvw(role="advisory", outcome="nits",
                       findings=[fnd("pr:7:x", severity="Low")])))
        self.assertEqual(open_findings(records), {})


class TestInconclusive(unittest.TestCase):
    def test_retry_allowed_under_bound(self):
        # #557/#552: sandbox blocked all network access; "this is not an
        # approval and not a request-for-changes" now has a state.
        records, _ = parse_records(bodies(
            req(),
            rvw(rid="rvw-1", outcome="inconclusive", at=ts(10))))
        self.assertTrue(can_retry(records, "req-1"))

    def test_retry_exhausted_at_bound(self):
        records, _ = parse_records(bodies(
            req(),
            *[rvw(rid=f"rvw-{i}", outcome="inconclusive", at=ts(10 + i))
              for i in range(3)]))
        self.assertFalse(can_retry(records, "req-1"))

    def test_inconclusive_does_not_increment_rounds(self):
        records, _ = parse_records(bodies(
            req(),
            rvw(rid="rvw-1", outcome="inconclusive", at=ts(10)),
            rvw(rid="rvw-2", outcome="blocker", findings=[fnd("pr:7:a")],
                at=ts(11))))
        self.assertEqual(rebuild_counters(records)["rounds"], 1)

    def test_inconclusive_does_not_reset_no_progress_timer(self):
        now = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
        records, _ = parse_records(bodies(
            req(at=ts(9)),
            rvw(rid="rvw-1", outcome="inconclusive", at=ts(19, 55))))
        # Latest progress is the 09:00 request; the 19:55 inconclusive
        # review must not have reset the clock.
        self.assertTrue(no_progress(records, now, timeout_seconds=3600))

    def test_conclusive_review_resets_the_timer(self):
        now = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
        records, _ = parse_records(bodies(
            req(at=ts(9)), rvw(outcome="approved", at=ts(19, 55))))
        self.assertFalse(no_progress(records, now, timeout_seconds=3600))


class TestCiClassification(unittest.TestCase):
    def test_empty_check_list_is_absent_not_green(self):
        # all([]) is True; 4 of 9 actionable repos have no CI at all.
        self.assertEqual(classify_ci([]), "absent")

    def test_unreadable_rollup_is_unavailable(self):
        self.assertEqual(classify_ci(None), "unavailable")

    def test_all_passing_is_green(self):
        checks = [{"name": "tests", "status": "COMPLETED", "conclusion": "success"}]
        self.assertEqual(classify_ci(checks), "green")

    def test_pending_check_is_unavailable(self):
        checks = [{"name": "tests", "status": "IN_PROGRESS", "conclusion": None}]
        self.assertEqual(classify_ci(checks), "unavailable")

    def test_unattributed_failure(self):
        checks = [{"name": "tests", "status": "COMPLETED", "conclusion": "failure"}]
        self.assertEqual(classify_ci(checks), "failing-unattributed")

    def test_attributed_failure(self):
        checks = [{"name": "tests", "status": "COMPLETED", "conclusion": "failure"}]
        self.assertEqual(classify_ci(checks, {"tests"}), "failing-attributed")


class TestDiffScoping(unittest.TestCase):
    def test_out_of_diff_blocker_is_demoted(self):
        # #554 regression: a one-line SHA repin drew "Blocking: live secret
        # committed to git history" because a shallow clone made git diff
        # fall back to the empty tree. A path outside the changed-file set
        # cannot be a blocker — mechanically, for every reviewer.
        findings = [
            fnd("pr:554:secret", severity="High",
                path="config/app-settings/alfred/prefs.plist"),
        ]
        in_scope, demoted = validate_blocking_paths(
            findings, [".github/workflows/claude-code-review.yml"])
        self.assertEqual(in_scope, [])
        self.assertEqual([f["id"] for f in demoted], ["pr:554:secret"])

    def test_in_diff_blocker_survives(self):
        findings = [fnd("pr:7:a", path="src/app.py")]
        in_scope, demoted = validate_blocking_paths(findings, ["src/app.py"])
        self.assertEqual([f["id"] for f in in_scope], ["pr:7:a"])
        self.assertEqual(demoted, [])

    def test_non_blocking_out_of_diff_passes_through(self):
        findings = [fnd("pr:7:n", severity="Nit", path="README.md")]
        in_scope, demoted = validate_blocking_paths(findings, ["src/app.py"])
        self.assertEqual(len(in_scope), 1)
        self.assertEqual(demoted, [])

    def test_effective_severities_exclude_demoted(self):
        # The whole-repo "diff" produced only out-of-scope findings; the
        # verdict they may influence is approved, not blocker.
        findings = [
            fnd("pr:554:secret", severity="High", path="vendored/prefs.plist"),
        ]
        sev = effective_severities(findings, ["workflow.yml"])
        self.assertEqual(sev, [])


class TestShipReady(unittest.TestCase):
    def _base(self):
        return bodies(
            req(),
            rvw(outcome="approved"))

    def test_all_gates_pass(self):
        records, _ = parse_records(self._base())
        ok, reasons = ship_ready_eligible(records, "a" * 40, "green", "fresh")
        self.assertTrue(ok, reasons)

    def test_absent_ci_blocks(self):
        records, _ = parse_records(self._base())
        ok, reasons = ship_ready_eligible(records, "a" * 40, "absent", "fresh")
        self.assertFalse(ok)
        self.assertTrue(any("ci_state" in r for r in reasons))

    def test_attributed_failure_passes_ci_gate(self):
        records, _ = parse_records(self._base())
        ok, _ = ship_ready_eligible(
            records, "a" * 40, "failing-attributed", "fresh")
        self.assertTrue(ok)

    def test_open_findings_block(self):
        records, _ = parse_records(bodies(
            req(), rvw(outcome="approved", findings=[fnd("pr:7:a")])))
        ok, reasons = ship_ready_eligible(records, "a" * 40, "green", "fresh")
        self.assertFalse(ok)

    def test_stale_head_blocks(self):
        records, _ = parse_records(self._base())
        ok, _ = ship_ready_eligible(records, "b" * 40, "green", "fresh")
        self.assertFalse(ok)

    def test_terminal_record_blocks(self):
        records, _ = parse_records(self._base() + bodies(trm()))
        ok, reasons = ship_ready_eligible(records, "a" * 40, "green", "fresh")
        self.assertFalse(ok)
        self.assertTrue(any("terminal" in r for r in reasons))

    def test_stale_base_blocks(self):
        records, _ = parse_records(self._base())
        ok, _ = ship_ready_eligible(records, "a" * 40, "green", "behind")
        self.assertFalse(ok)


class TestTerminals(unittest.TestCase):
    def test_superseded_and_probe_are_representable(self):
        # ~20% of real outcomes were closed-not-merged with no representation.
        for state in ("superseded", "probe"):
            records, _ = parse_records(bodies(trm(tid=f"t-{state}", state=state)))
            self.assertEqual(terminal_state(records)["state"], state)

    def test_latest_terminal_wins(self):
        records, _ = parse_records(bodies(
            trm(tid="t-1", state="escalated", at=ts(9)),
            trm(tid="t-2", state="superseded", at=ts(10))))
        self.assertEqual(terminal_state(records)["state"], "superseded")


class TestRebuildCounters(unittest.TestCase):
    def test_counters_survive_context_loss(self):
        # The state file is a cache: deleting it and rebuilding from the PR
        # thread must yield identical counters.
        records, _ = parse_records(bodies(
            req(rid="req-1", at=ts(9)),
            rvw(rid="rvw-1", outcome="blocker", findings=[fnd("pr:7:a")],
                at=ts(10)),
            dsp("d-1", "pr:7:a", status="fixed", at=ts(11)),
            req(rid="req-2", at=ts(12)),
            rvw(rid="rvw-2", request_id="req-2", outcome="approved", at=ts(13))))
        self.assertEqual(rebuild_counters(records)["rounds"], 2)

    def test_pushback_only_rounds_counted(self):
        records, _ = parse_records(bodies(
            req(rid="req-1", at=ts(9)),
            rvw(rid="rvw-1", outcome="nits",
                findings=[fnd("pr:7:a", severity="Nit")], at=ts(10)),
            dsp("d-1", "pr:7:a", status="contested", at=ts(11)),
            rvw(rid="rvw-2", outcome="nits",
                findings=[fnd("pr:7:b", severity="Nit")], at=ts(12)),
            dsp("d-2", "pr:7:b", status="contested", at=ts(13))))
        self.assertEqual(rebuild_counters(records)["pushback_only_rounds"], 2)

    def test_a_fix_resets_pushback_count(self):
        records, _ = parse_records(bodies(
            req(rid="req-1", at=ts(9)),
            rvw(rid="rvw-1", outcome="nits",
                findings=[fnd("pr:7:a", severity="Nit")], at=ts(10)),
            dsp("d-1", "pr:7:a", status="fixed", at=ts(11))))
        self.assertEqual(rebuild_counters(records)["pushback_only_rounds"], 0)


if __name__ == "__main__":
    unittest.main()
