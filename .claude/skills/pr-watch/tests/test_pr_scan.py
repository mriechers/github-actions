import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pr_scan import (  # noqa: E402
    format_marker,
    parse_last_reviewed_sha,
    is_opted_out,
    classify,
    has_agent_feedback,
    needs_attention,
    scan,
    resolve_owners,
)


class TestMarker(unittest.TestCase):
    def test_format_marker(self):
        self.assertEqual(format_marker("abc1234"), "<!-- pr-watch: sha=abc1234 -->")

    def test_parse_none_when_absent(self):
        comments = [{"body": "nice work"}, {"body": "lgtm"}]
        self.assertIsNone(parse_last_reviewed_sha(comments))

    def test_parse_returns_last_marker(self):
        comments = [
            {"body": "first pass\n<!-- pr-watch: sha=1111111 -->"},
            {"body": "second pass\n<!-- pr-watch: sha=2222222 -->"},
        ]
        self.assertEqual(parse_last_reviewed_sha(comments), "2222222")

    def test_parse_tolerates_missing_body(self):
        comments = [{"author": {"login": "x"}}, {"body": None}]
        self.assertIsNone(parse_last_reviewed_sha(comments))


class TestOptOut(unittest.TestCase):
    def test_opted_out_true(self):
        self.assertTrue(is_opted_out({"labels": [{"name": "no-pr-watch"}]}))

    def test_opted_out_case_insensitive(self):
        self.assertTrue(is_opted_out({"labels": [{"name": "No-PR-Watch"}]}))

    def test_not_opted_out(self):
        self.assertFalse(is_opted_out({"labels": [{"name": "bug"}]}))


class TestClassify(unittest.TestCase):
    def test_new_when_no_marker(self):
        self.assertEqual(classify("abc", None), "new")

    def test_changed_when_sha_differs(self):
        self.assertEqual(classify("abc", "def"), "changed")

    def test_current_when_sha_matches(self):
        self.assertEqual(classify("abc", "abc"), "current")

    def test_truncated_marker_matching_head_prefix_is_current(self):
        # The marker-truncation trap: a 7-char marker could never string-equal
        # the 40-char head, so the PR read "changed" forever and was
        # re-reviewed every tick. A short marker that prefixes the head means
        # "already reviewed".
        head = "1234567" + "a" * 33
        self.assertEqual(classify(head, "1234567"), "current")

    def test_truncated_marker_not_matching_head_is_changed(self):
        head = "9999999" + "a" * 33
        self.assertEqual(classify(head, "1234567"), "changed")

    def test_full_length_marker_still_requires_equality(self):
        head = "a" * 40
        stale = "a" * 39 + "b"
        self.assertEqual(classify(head, stale), "changed")


class TestV1Records(unittest.TestCase):
    def _record_body(self, sha, rid="rvw-1"):
        return (
            '<!-- pr-review:v1 {"kind":"review","id":"%s","request_id":"req-1",'
            '"role":"fallback","reviewer":"pr-watch","dispatched_sha":"%s",'
            '"reviewed_sha":"%s","outcome":"approved","findings":[],'
            '"reviewed_at":"2026-08-06T10:00:00Z"} -->' % (rid, sha, sha)
        )

    def test_v1_record_wins_over_legacy_marker(self):
        v1_sha = "c" * 40
        comments = [
            {"body": "old <!-- pr-watch: sha=" + "b" * 40 + " -->"},
            {"body": "new review\n" + self._record_body(v1_sha)},
        ]
        self.assertEqual(parse_last_reviewed_sha(comments), v1_sha)

    def test_legacy_marker_still_read_without_records(self):
        comments = [{"body": "x <!-- pr-watch: sha=" + "b" * 40 + " -->"}]
        self.assertEqual(parse_last_reviewed_sha(comments), "b" * 40)

    def test_newest_record_wins(self):
        comments = [
            {"body": self._record_body("d" * 40, rid="rvw-1")},
            {"body": self._record_body("e" * 40, rid="rvw-2")},
        ]
        self.assertEqual(parse_last_reviewed_sha(comments), "e" * 40)

    def test_record_counts_as_agent_feedback(self):
        comments = [{"body": self._record_body("f" * 40),
                     "author": {"login": "mark"}}]
        self.assertTrue(has_agent_feedback(comments, []))


class TestAgentFeedback(unittest.TestCase):
    def test_marker_counts_as_feedback(self):
        comments = [{"body": "x <!-- pr-watch: sha=1111111 -->",
                     "author": {"login": "mark"}}]
        self.assertTrue(has_agent_feedback(comments, []))

    def test_bot_comment_counts_as_feedback(self):
        comments = [{"body": "review", "author": {"login": "github-actions[bot]"}}]
        self.assertTrue(has_agent_feedback(comments, []))

    def test_bot_review_counts_as_feedback(self):
        reviews = [{"body": "lgtm", "author": {"login": "claude[bot]"}}]
        self.assertTrue(has_agent_feedback([], reviews))

    def test_no_feedback_when_only_humans(self):
        comments = [{"body": "thanks", "author": {"login": "mark"}}]
        self.assertFalse(has_agent_feedback(comments, []))


class TestNeedsAttention(unittest.TestCase):
    def _pr(self, **kw):
        base = {"isDraft": False, "labels": [], "comments": [], "reviews": []}
        base.update(kw)
        return base

    def test_flagged_when_unreviewed(self):
        self.assertTrue(needs_attention(self._pr()))

    def test_not_flagged_when_draft(self):
        self.assertFalse(needs_attention(self._pr(isDraft=True)))

    def test_not_flagged_when_optout(self):
        self.assertFalse(needs_attention(self._pr(labels=[{"name": "no-pr-watch"}])))

    def test_not_flagged_when_has_feedback(self):
        pr = self._pr(comments=[{"body": "r", "author": {"login": "claude[bot]"}}])
        self.assertFalse(needs_attention(pr))


class TestScan(unittest.TestCase):
    def setUp(self):
        self.prs = [
            {"repository": {"nameWithOwner": "o/a"}, "number": 1, "title": "t1",
             "url": "u1", "isDraft": False, "labels": [],
             "createdAt": "2026-07-15T00:00:00Z",
             "updatedAt": "2026-07-16T00:00:00Z"},
            {"repository": {"nameWithOwner": "o/b"}, "number": 2, "title": "t2",
             "url": "u2", "isDraft": False, "labels": [],
             "createdAt": "2026-07-15T00:00:00Z",
             "updatedAt": "2026-07-16T00:00:00Z"},
        ]
        self.details = {
            "o/a#1": {"headRefOid": "aaaaaaa", "comments": [], "reviews": [],
                      "state": "OPEN"},
            "o/b#2": {"headRefOid": "bbbbbbb",
                      "comments": [{"body": "done <!-- pr-watch: sha=bbbbbbb -->",
                                    "author": {"login": "mark"}}],
                      "reviews": [], "state": "OPEN"},
        }

    def _detail_fn(self, nwo, number):
        return self.details[f"{nwo}#{number}"]

    def test_scan_classifies_and_flags(self):
        records = scan(self.prs, self._detail_fn)
        self.assertEqual(records[0]["repo"], "o/a")
        self.assertEqual(records[0]["action"], "new")
        self.assertTrue(records[0]["needs_attention"])
        self.assertEqual(records[1]["action"], "current")
        self.assertFalse(records[1]["needs_attention"])
        self.assertEqual(records[1]["last_reviewed_sha"], "bbbbbbb")
        self.assertEqual(records[0]["state"], "OPEN")
        self.assertEqual(records[0]["updated_at"], "2026-07-16T00:00:00Z")
        self.assertEqual(records[0]["created_at"], "2026-07-15T00:00:00Z")

    def test_scan_backlog_filters_to_needs_attention(self):
        records = scan(self.prs, self._detail_fn, backlog=True)
        self.assertEqual([r["number"] for r in records], [1])


class TestResolveOwners(unittest.TestCase):
    def test_empty_falls_back_to_defaults(self):
        self.assertEqual(resolve_owners(""),
                         ["mriechers", "public-media-work", "Wonder-Cabinet-Productions"])

    def test_parses_and_strips(self):
        self.assertEqual(resolve_owners("a, b ,c"), ["a", "b", "c"])

    def test_single_owner(self):
        self.assertEqual(resolve_owners("mriechers"), ["mriechers"])


if __name__ == "__main__":
    unittest.main()
