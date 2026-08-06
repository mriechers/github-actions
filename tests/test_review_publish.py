import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import review_publish  # noqa: E402
from review_publish import (  # noqa: E402
    VERDICTS,
    PublishError,
    apply_diff_scoping,
    build_request_record,
    build_review_record,
    decide_eligibility,
    parse_structured_output,
)

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", ".claude", "skills", "pr-watch", "scripts"))
import pr_label  # noqa: E402
import pr_record  # noqa: E402


def finding(fid="pr:7:a", severity="High", path="src/app.py"):
    return {"id": fid, "severity": severity, "path": path,
            "summary": "a finding"}


class TestVerdictTables(unittest.TestCase):
    def test_every_verdict_maps_to_a_canonical_review_state(self):
        for label, _, _ in VERDICTS.values():
            self.assertIn(label, pr_label.REVIEW_STATES)

    def test_every_outcome_is_a_protocol_outcome(self):
        for _, _, outcome in VERDICTS.values():
            self.assertIn(outcome, pr_record.OUTCOMES)

    def test_inconclusive_is_a_supported_verdict(self):
        self.assertIn("inconclusive", VERDICTS)
        label, conclusion, outcome = VERDICTS["inconclusive"]
        self.assertEqual(label, "review:inconclusive")
        self.assertEqual(conclusion, "neutral")
        self.assertEqual(outcome, "inconclusive")


class TestParseStructuredOutput(unittest.TestCase):
    def test_valid_minimal(self):
        result = parse_structured_output(
            json.dumps({"verdict": "approve", "summary": "clean"}))
        self.assertEqual(result["verdict"], "approve")
        self.assertEqual(result["findings"], [])

    def test_invalid_json_raises(self):
        with self.assertRaises(PublishError):
            parse_structured_output("not json")

    def test_extra_keys_rejected(self):
        with self.assertRaises(PublishError):
            parse_structured_output(json.dumps(
                {"verdict": "approve", "summary": "s", "labels": ["x"]}))

    def test_unknown_verdict_rejected(self):
        with self.assertRaises(PublishError):
            parse_structured_output(json.dumps(
                {"verdict": "ship_it", "summary": "s"}))

    def test_empty_summary_rejected(self):
        with self.assertRaises(PublishError):
            parse_structured_output(json.dumps(
                {"verdict": "approve", "summary": "   "}))

    def test_oversize_summary_rejected(self):
        with self.assertRaises(PublishError):
            parse_structured_output(json.dumps(
                {"verdict": "approve", "summary": "x" * 60001}))

    def test_finding_shape_enforced(self):
        with self.assertRaises(PublishError):
            parse_structured_output(json.dumps(
                {"verdict": "comment", "summary": "s",
                 "findings": [{"severity": "High"}]}))

    def test_findings_accepted(self):
        result = parse_structured_output(json.dumps(
            {"verdict": "request_changes", "summary": "s",
             "findings": [finding()]}))
        self.assertEqual(len(result["findings"]), 1)


class TestDiffScoping(unittest.TestCase):
    def test_pr554_whole_repo_blocker_downgrades(self):
        # #554 regression: a one-line SHA repin drew a "live secret" blocker
        # citing a vendored file nowhere near the diff. The blocker demotes
        # to a follow-up and the verdict downgrades to comment.
        verdict, in_scope, demoted = apply_diff_scoping(
            "request_changes",
            [finding("pr:554:secret", path="vendored/alfred/prefs.plist")],
            [".github/workflows/claude-code-review.yml"])
        self.assertEqual(verdict, "comment")
        self.assertEqual(in_scope, [])
        self.assertEqual([f["id"] for f in demoted], ["pr:554:secret"])

    def test_in_diff_blocker_keeps_request_changes(self):
        verdict, in_scope, demoted = apply_diff_scoping(
            "request_changes", [finding(path="src/app.py")], ["src/app.py"])
        self.assertEqual(verdict, "request_changes")
        self.assertEqual(len(in_scope), 1)
        self.assertEqual(demoted, [])

    def test_empty_changed_set_means_inconclusive(self):
        # #552/#557: the diff was unobtainable. Blocking against an
        # unobtainable diff is exactly the false-blocker mechanism; the
        # honest state is inconclusive.
        verdict, in_scope, demoted = apply_diff_scoping(
            "request_changes", [finding()], [])
        self.assertEqual(verdict, "inconclusive")
        self.assertEqual(in_scope, [])
        self.assertEqual(len(demoted), 1)

    def test_approve_passes_through(self):
        verdict, in_scope, demoted = apply_diff_scoping("approve", [], ["a"])
        self.assertEqual(verdict, "approve")
        self.assertEqual((in_scope, demoted), ([], []))

    def test_mixed_findings_keep_block_when_one_is_in_scope(self):
        verdict, in_scope, demoted = apply_diff_scoping(
            "request_changes",
            [finding("pr:7:in", path="src/app.py"),
             finding("pr:7:out", path="elsewhere.py")],
            ["src/app.py"])
        self.assertEqual(verdict, "request_changes")
        self.assertEqual([f["id"] for f in in_scope], ["pr:7:in"])
        self.assertEqual([f["id"] for f in demoted], ["pr:7:out"])


class TestRecords(unittest.TestCase):
    def _publication(self):
        return {"verdict": "request_changes", "event": "REQUEST_CHANGES",
                "label": "review:blocker", "summary": "s",
                "check_conclusion": "failure", "outcome": "blocker",
                "findings": [dict(finding(), status="open")],
                "demoted_findings": []}

    def test_review_record_is_valid_protocol(self):
        record = build_review_record(
            self._publication(), "7", "a" * 40, "12345",
            "2026-08-06T12:00:00Z")
        marker = pr_record.serialize(record)  # raises if malformed
        records, errors = pr_record.parse_records([marker])
        self.assertEqual(errors, [])
        self.assertEqual(records[0]["role"], "primary")
        self.assertEqual(records[0]["reviewer"], "claude-review-action")

    def test_request_record_is_valid_protocol(self):
        record = build_request_record("7", "a" * 40, "12345",
                                      "2026-08-06T12:00:00Z")
        records, errors = pr_record.parse_records([pr_record.serialize(record)])
        self.assertEqual(errors, [])
        self.assertEqual(records[0]["selected_role"], "primary")

    def test_records_make_the_action_verdict_current(self):
        # End-to-end: the Action's own request+review pair yields a current
        # verdict for the head it reviewed.
        pub = self._publication()
        markers = [
            pr_record.serialize(build_request_record(
                "7", "a" * 40, "12345", "2026-08-06T12:00:00Z")),
            pr_record.serialize(build_review_record(
                pub, "7", "a" * 40, "12345", "2026-08-06T12:00:01Z")),
        ]
        records, errors = pr_record.parse_records(markers)
        self.assertEqual(errors, [])
        self.assertEqual(pr_record.verdict_for(records, "a" * 40), "blocker")
        self.assertIn("pr:7:a", pr_record.open_findings(records))


class TestComposeIntegration(unittest.TestCase):
    def test_compose_embeds_records_and_downgrades_without_app_token(self):
        pub = {"verdict": "approve", "event": "APPROVE",
               "label": "review:approved", "summary": "clean",
               "check_conclusion": "success", "outcome": "approved",
               "findings": [], "demoted_findings": []}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {
                    "HAS_APP_TOKEN": "false",
                    "PR_HEAD_SHA": "a" * 40,
                    "PR_NUMBER": "7",
                    "GH_RUN_ID": "99",
                    "REVIEWED_AT": "2026-08-06T12:00:00Z",
                }):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with open("claude-review-publication.json", "w") as f:
                    json.dump(pub, f)
                self.assertEqual(review_publish.cmd_compose(), 0)
                with open("claude-review-request.json") as f:
                    request = json.load(f)
                with open("claude-review-check.json") as f:
                    check = json.load(f)
            finally:
                os.chdir(cwd)
        self.assertEqual(request["event"], "COMMENT")
        self.assertIn("formal approval was not submitted", request["body"])
        records, errors = pr_record.parse_records([request["body"]])
        self.assertEqual(errors, [])
        self.assertEqual({r["kind"] for r in records}, {"request", "review"})
        self.assertEqual(check["conclusion"], "success")
        self.assertEqual(check["name"], "Claude autonomous review")


class TestEligibility(unittest.TestCase):
    def _check(self, title, status="completed"):
        return {"name": "Claude autonomous review", "status": status,
                "output": {"title": title}}

    def test_no_checks_publishes(self):
        publish, _ = decide_eligibility([])
        self.assertTrue(publish)

    def test_conclusive_check_skips(self):
        publish, reason = decide_eligibility([self._check("Review: approve")])
        self.assertFalse(publish)
        self.assertIn("conclusive", reason)

    def test_inconclusive_allows_retry(self):
        publish, reason = decide_eligibility(
            [self._check("Review: inconclusive")])
        self.assertTrue(publish)
        self.assertIn("retrying", reason)

    def test_retry_budget_exhausts(self):
        publish, reason = decide_eligibility(
            [self._check("Review: inconclusive")] * 3)
        self.assertFalse(publish)
        self.assertIn("exhausted", reason)

    def test_other_check_names_ignored(self):
        publish, _ = decide_eligibility(
            [{"name": "tests", "status": "completed",
              "output": {"title": "Review: approve"}}])
        self.assertTrue(publish)

    def test_incomplete_run_does_not_skip(self):
        publish, _ = decide_eligibility(
            [self._check("Review: approve", status="in_progress")])
        self.assertTrue(publish)


class TestWorkflowConsistency(unittest.TestCase):
    """Backstop: while any label/verdict strings remain inlined in
    claude-review.yml, they must agree with the canonical definitions."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..",
                            ".github", "workflows", "claude-review.yml")
        with open(path, encoding="utf-8") as f:
            cls.workflow = f.read()

    def test_workflow_review_labels_are_canonical(self):
        import re
        for label in set(re.findall(r"review:[a-z-]+", self.workflow)):
            self.assertIn(label, pr_label.REVIEW_STATES,
                          f"{label} inlined in claude-review.yml is not in "
                          f"pr_label.REVIEW_STATES")

    def test_workflow_schema_verdicts_match_publisher(self):
        for verdict in VERDICTS:
            self.assertIn(f'"{verdict}"', self.workflow,
                          f"verdict {verdict} missing from the workflow's "
                          f"structured-output schema")


if __name__ == "__main__":
    unittest.main()
