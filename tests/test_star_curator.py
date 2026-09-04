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
    filing_target,
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
        # The prefix is checked against the bare repo name, so an owner called
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

    def test_keywords_never_match_against_the_owner(self):
        # `prefixes` deliberately ignores the owner so `awesome-corp` cannot
        # sweep its whole output into one list. Keywords must not reopen that
        # door: an owner named `glitch-labs` publishing a database is still a
        # database.
        rules = {"lists": {"Glitch": {"keywords": ["glitch"]}}}
        r = repo(full_name="glitch-labs/postgres-pool", description="a database pool")
        self.assertEqual(route(r, rules), [])

    def test_keywords_match_as_substrings_not_words(self):
        # Pinning the semantics, because they are sharp. A short keyword hits
        # inside longer words: `art` files `startech` and `Chartbuilder` into a
        # glitch-art list. A single match FILES rather than reports, so this is
        # silent misrouting, not a noisy false positive. If this ever becomes
        # word-boundary matching, that must be a deliberate change with the
        # rules files regenerated — not an incidental refactor.
        rules = {"lists": {"Glitch": {"keywords": ["art"]}}}
        self.assertEqual(route(repo(full_name="o/startech-enclosure"), rules), ["Glitch"])


class TestFilingTarget(unittest.TestCase):
    def test_one_rule_one_existing_list_files(self):
        self.assertEqual(filing_target(["HA"], ["HA"]), "HA")

    def test_two_rules_never_file_even_when_only_one_list_exists(self):
        # The blocker this function exists for. `present` alone is 1 here, so
        # the old gate filed it — silently picking one of two matching rules,
        # the single thing the engine promises never to do. Which lists have
        # been created is not evidence about which rule was meant.
        self.assertIsNone(filing_target(["Glitch", "Awesome"], ["Glitch"]))

    def test_two_rules_two_lists_is_ambiguous(self):
        self.assertIsNone(filing_target(["Glitch", "Awesome"], ["Glitch", "Awesome"]))

    def test_one_rule_naming_a_missing_list_does_not_file(self):
        self.assertIsNone(filing_target(["HA"], []))

    def test_no_rules_do_not_file(self):
        self.assertIsNone(filing_target([], []))


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

    def test_keep_lists_exempts_a_whole_deliberately_historical_list(self):
        rules = dict(RULES, keep_lists=["Attic"])
        lists = {"Attic": {"id": "L1", "items": {"a/b", "c/d"}}}
        stars = [repo(full_name="a/b", archived=True), repo(full_name="e/f", archived=True)]
        # Only the repo outside the exempt list is reported.
        self.assertEqual(rot_signals(stars, rules, lists), [("e/f", "archived upstream")])

    def test_keep_lists_naming_a_missing_list_is_not_fatal(self):
        rules = dict(RULES, keep_lists=["Nope"])
        out = rot_signals([repo(full_name="a/b", archived=True)], rules, {})
        self.assertEqual(out, [("a/b", "archived upstream")])


class TestReport(unittest.TestCase):
    def test_clean_run_says_so(self):
        self.assertIn("Nothing to report", build_report([], [], [], [], False))

    def test_dry_run_is_announced(self):
        out = build_report([("a/b", "HA Projects")], [], [], [], True)
        self.assertIn("Dry run", out)

    def test_dry_run_says_would_file_not_filed(self):
        # The banner says nothing was filed; the section heading must agree.
        out = build_report([("a/b", "HA Projects")], [], [], [], True)
        self.assertIn("Would file", out)
        self.assertNotIn("Filed automatically", out)

    def test_degraded_run_says_would_file_too(self):
        out = build_report([("a/b", "HA")], [], [], [], False, "api gone")
        self.assertIn("Would file", out)
        self.assertNotIn("Filed automatically", out)

    def test_a_real_run_says_filed(self):
        out = build_report([("a/b", "HA Projects")], [], [], [], False)
        self.assertIn("Filed automatically", out)

    def test_ambiguous_entry_explains_why(self):
        out = build_report([], [(repo(full_name="a/b"), [], [])], [], [], False)
        self.assertIn("no rule matched", out)

    def test_multi_match_names_the_candidates(self):
        out = build_report([], [(repo(full_name="a/b"), ["X", "Y"], ["X", "Y"])], [], [], False)
        self.assertIn("matches X, Y", out)

    def test_match_on_a_missing_list_says_so_instead_of_no_match(self):
        # First-time setup is exactly when a rule names a list that has not
        # been created yet. Reporting that as "no rule matched" sends someone
        # to debug their rules file when the rules are fine.
        out = build_report([], [(repo(full_name="a/b"), ["HA Projects"], [])], [], [], False)
        self.assertIn("no list of that name exists", out)
        self.assertNotIn("no rule matched", out)


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


class TestDegradedMode(unittest.TestCase):
    def test_report_announces_that_filing_is_off(self):
        out = build_report([], [], [], [], False, "mutation is gone")
        self.assertIn("Filing is disabled", out)
        self.assertIn("mutation is gone", out)

    def test_report_still_renders_its_findings_alongside_the_warning(self):
        # The whole point of the degraded mode: the read path keeps its value.
        out = build_report([], [], [("a/b", "archived upstream")], [], False, "gone")
        self.assertIn("Filing is disabled", out)
        self.assertIn("a/b", out)


class TestLoadRules(unittest.TestCase):
    def _write(self, text, suffix=".json"):
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_a_non_mapping_is_named_not_a_traceback(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(self._write("[1, 2, 3]"))
        self.assertIn("must be a mapping", str(cm.exception))

    def test_a_missing_file_is_named(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules("/nonexistent/star-rules.yml")
        self.assertIn("not found", str(cm.exception))


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
