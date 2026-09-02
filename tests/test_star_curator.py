import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import star_curator  # noqa: E402
from star_curator import (  # noqa: E402
    REQUIRED_MUTATIONS,
    build_report,
    fetch_lists,
    rot_signals,
    route,
)


def _items(names, has_next=False, cursor=None):
    return {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [{"nameWithOwner": n} for n in names],
    }


def repo(full_name="o/r", description="", topics=(), pushed_at="2026-01-01", archived=False):
    return {
        "full_name": full_name,
        "node_id": "R_x",
        "description": description,
        "language": "Python",
        "topics": [t.lower() for t in topics],
        "archived": archived,
        "pushed_at": pushed_at,
        "stars": 0,
    }


RULES = {
    "stale_before_year": 2023,
    "lists": {
        "HA Projects": {"topics": ["home-assistant", "hacs"]},
        "Glitch Art & Corruption": {"topics": ["glitch", "datamosh"]},
        "Awesome Lists": {"prefixes": ["awesome-"], "keywords": ["curated list"]},
        "Agent tooling": {"topics": ["mcp"], "keywords": ["model context protocol"]},
    },
}


class TestRoute(unittest.TestCase):
    def test_topic_match_is_unambiguous(self):
        r = repo(topics=["home-assistant", "python"])
        self.assertEqual(route(r, RULES), ["HA Projects"])

    def test_keyword_matches_description(self):
        r = repo(description="A Model Context Protocol server for things")
        self.assertEqual(route(r, RULES), ["Agent tooling"])

    def test_prefix_matches_repo_name_not_owner(self):
        # The prefix is checked against the full name, so an owner called
        # "awesome-corp" must not sweep every repo they publish into the list.
        self.assertEqual(route(repo(full_name="awesome-corp/widget"), RULES), [])

    def test_no_match_is_ambiguous(self):
        self.assertEqual(route(repo(description="a database"), RULES), [])

    def test_multiple_matches_are_reported_not_guessed(self):
        # A repo that is both a glitch tool and an awesome list must NOT be
        # filed automatically — picking one is exactly the judgement call the
        # engine refuses to make.
        r = repo(topics=["glitch"], description="A curated list of tools")
        self.assertEqual(sorted(route(r, RULES)), ["Awesome Lists", "Glitch Art & Corruption"])

    def test_case_is_normalised_across_topics_and_text(self):
        r = repo(description="MODEL CONTEXT PROTOCOL server")
        self.assertEqual(route(r, RULES), ["Agent tooling"])


class TestRotSignals(unittest.TestCase):
    def test_archived_is_flagged(self):
        out = rot_signals([repo(full_name="a/b", archived=True)], RULES)
        self.assertEqual(out, [("a/b", "archived upstream")])

    def test_stale_push_is_flagged(self):
        out = rot_signals([repo(full_name="a/b", pushed_at="2019-04-02")], RULES)
        self.assertEqual(out, [("a/b", "no push since 2019-04-02")])

    def test_keep_list_silences_a_signal(self):
        rules = dict(RULES, keep=["A/B"])
        self.assertEqual(rot_signals([repo(full_name="a/b", archived=True)], rules), [])

    def test_fresh_repo_is_quiet(self):
        self.assertEqual(rot_signals([repo(pushed_at="2026-08-01")], RULES), [])


class TestReport(unittest.TestCase):
    def test_clean_run_says_so(self):
        self.assertIn("Nothing to report", build_report([], [], [], [], False))

    def test_dry_run_is_announced(self):
        out = build_report([("a/b", "HA Projects")], [], [], [], True)
        self.assertIn("Dry run", out)

    def test_ambiguous_entry_explains_why(self):
        out = build_report([], [(repo(full_name="a/b"), [])], [], [], False)
        self.assertIn("no rule matched", out)

    def test_multi_match_names_the_candidates(self):
        out = build_report([], [(repo(full_name="a/b"), ["X", "Y"])], [], [], False)
        self.assertIn("matches X, Y", out)


class TestFetchListsPagination(unittest.TestCase):
    """A member missed here looks unfiled, and filing it REPLACES its
    membership — so a read cap is data loss, not a missing row."""

    def test_items_beyond_the_first_page_are_collected(self):
        pages = [
            {
                "viewer": {
                    "lists": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"id": "L1", "name": "Big", "items": _items(["a/1"], True, "c1")}
                        ],
                    }
                }
            },
            {"node": {"items": _items(["a/2"], True, "c2")}},
            {"node": {"items": _items(["a/3"])}},
        ]
        with mock.patch.object(star_curator, "graphql", side_effect=pages):
            out = fetch_lists("tok")
        self.assertEqual(out["Big"]["items"], {"a/1", "a/2", "a/3"})

    def test_lists_beyond_the_first_page_are_collected(self):
        pages = [
            {
                "viewer": {
                    "lists": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "p1"},
                        "nodes": [{"id": "L1", "name": "One", "items": _items(["a/1"])}],
                    }
                }
            },
            {
                "viewer": {
                    "lists": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"id": "L2", "name": "Two", "items": _items(["b/2"])}],
                    }
                }
            },
        ]
        with mock.patch.object(star_curator, "graphql", side_effect=pages):
            out = fetch_lists("tok")
        self.assertEqual(sorted(out), ["One", "Two"])
        self.assertEqual(out["Two"]["items"], {"b/2"})

    def test_single_page_makes_no_follow_up_query(self):
        page = {
            "viewer": {
                "lists": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"id": "L1", "name": "Small", "items": _items(["a/1"])}],
                }
            }
        }
        with mock.patch.object(star_curator, "graphql", side_effect=[page]) as g:
            fetch_lists("tok")
        self.assertEqual(g.call_count, 1)


class TestSafetyContract(unittest.TestCase):
    def test_engine_does_not_depend_on_destructive_mutations(self):
        # The engine must never reach for these. If one shows up in
        # REQUIRED_MUTATIONS, filing has quietly grown the power to unstar or
        # restructure someone's lists.
        for forbidden in ("deleteUserList", "createUserList"):
            self.assertNotIn(forbidden, REQUIRED_MUTATIONS)

    def test_engine_source_never_calls_the_unstar_endpoint(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "star_curator.py"),
            encoding="utf-8",
        ).read()
        self.assertNotIn("DELETE", src)


if __name__ == "__main__":
    unittest.main()
