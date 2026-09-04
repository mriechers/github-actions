import json
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

    def test_topic_rules_are_case_insensitive(self):
        # Repo topics arrive lowercased. A capitalised rule matched nothing,
        # and did so silently — the repo just showed up under "needs a
        # decision" with a rules file that looked correct.
        rules = {"lists": {"HA": {"topics": ["Home-Assistant"]}}}
        self.assertEqual(route(repo(topics=["home-assistant"]), rules), ["HA"])

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
        out = build_report([("a/b", "HA Projects", False)], [], [], [], True)
        self.assertIn("Dry run", out)

    def test_dry_run_says_would_file_not_filed(self):
        # The banner says nothing was filed; the section heading must agree.
        out = build_report([("a/b", "HA Projects", False)], [], [], [], True)
        self.assertIn("Would file", out)
        self.assertNotIn("Filed automatically", out)

    def test_degraded_run_says_would_file_too(self):
        out = build_report([("a/b", "HA", False)], [], [], [], False, "api gone")
        self.assertIn("Would file", out)
        self.assertNotIn("Filed automatically", out)

    def test_a_real_run_says_filed(self):
        out = build_report([("a/b", "HA Projects", True)], [], [], [], False)
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


    def test_a_partly_missing_multi_match_names_both(self):
        # Two rules matched, one list exists. Naming only the survivor reads
        # as a single unambiguous match and leaves the reader asking why it
        # was not filed — the second rule is the whole answer.
        out = build_report([], [(repo(full_name="a/b"), ["Glitch", "Awesome"], ["Glitch"])],
                           [], [], False)
        self.assertIn("Glitch", out)
        self.assertIn("Awesome", out)
        self.assertIn("does not exist", out)


    def test_a_partial_filing_failure_still_reports_what_was_written(self):
        # The writes that landed cannot be rolled back, so the run must not
        # die silently holding that knowledge.
        out = build_report(
            [("a/b", "HA", True), ("c/d", "HA", False)], [], [], [], False,
            filing_error="HTTP 502",
        )
        self.assertIn("Filing stopped after 1 of 2", out)
        self.assertIn("HTTP 502", out)


    def test_a_partial_failure_keeps_past_tense_for_the_writes_that_landed(self):
        # The banner says the successful writes are committed. The heading
        # must not then call the whole batch "Would file".
        out = build_report(
            [("a/b", "HA", True), ("c/d", "HA", False)], [], [], [], False,
            filing_error="HTTP 502",
        )
        self.assertIn("Filed automatically", out)
        self.assertIn("_(not written)_", out)

    def test_an_api_failure_is_never_nothing_to_report(self):
        # The quietest possible output for the loudest possible problem.
        out = build_report([], [], [], [], False, list_api_error="schema gone")
        self.assertNotIn("Nothing to report", out)


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


class TestListApiGone(unittest.TestCase):
    """Degraded mode must fire for a missing mutation and NOTHING else."""

    def _schema(self, *mutations):
        return {"__schema": {"mutationType": {"fields": [{"name": m} for m in mutations]}}}

    def test_missing_mutation_raises_the_degrading_error(self):
        with mock.patch.object(star_curator, "graphql", return_value=self._schema("somethingElse")):
            with self.assertRaises(star_curator.ListApiGone):
                star_curator.assert_list_api("tok")

    def test_a_present_mutation_passes(self):
        with mock.patch.object(
            star_curator, "graphql", return_value=self._schema("updateUserListsForItem")
        ):
            star_curator.assert_list_api("tok")  # must not raise

    def test_an_auth_failure_is_not_a_schema_change(self):
        # A dead PAT must NOT be reported as "GitHub changed their schema" —
        # that sends someone to rewrite the engine when the fix is to rotate
        # a secret. ListApiGone is a subclass, so assert the exact type.
        boom = star_curator.ApiError("POST /graphql -> HTTP 401: bad credentials")
        with mock.patch.object(star_curator, "graphql", side_effect=boom):
            with self.assertRaises(star_curator.ApiError) as cm:
                star_curator.assert_list_api("tok")
        self.assertNotIsInstance(cm.exception, star_curator.ListApiGone)


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

    def test_lists_written_as_a_sequence_is_named(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(self._write('{"lists": ["HA", "Glitch"]}'))
        self.assertIn("must be a mapping", str(cm.exception))

    def test_a_scalar_keyword_string_is_refused(self):
        # THE safety check. `keywords: home assistant` (no brackets) leaves
        # Python iterating characters, so single-char keywords match nearly
        # every repo — and a single match files. On a real run that mass-
        # mis-files a collection into one list, and because filing replaces
        # membership it strips those repos out of where they belonged.
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(
                self._write('{"lists": {"HA": {"keywords": "home assistant"}}}')
            )
        self.assertIn("must be a list", str(cm.exception))

    def test_a_scalar_topics_string_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(self._write('{"lists": {"HA": {"topics": "hacs"}}}'))
        self.assertIn("must be a list", str(cm.exception))

    def test_a_list_header_with_nothing_under_it_is_named(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(self._write('{"lists": {"HA": null}}'))
        self.assertIn("HA", str(cm.exception))

    def test_a_non_string_matcher_value_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(self._write('{"lists": {"HA": {"topics": ["ok", 7]}}}'))
        self.assertIn("only strings", str(cm.exception))

    def test_a_non_numeric_oversize_is_named(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules(self._write('{"oversize": "lots", "lists": {}}'))
        self.assertIn("must be a number", str(cm.exception))

    def test_a_well_formed_file_passes(self):
        rules = star_curator.load_rules(
            self._write('{"oversize": 30, "lists": {"HA": {"topics": ["hacs"], "keywords": []}}}')
        )
        self.assertEqual(rules["lists"]["HA"]["topics"], ["hacs"])

    def test_a_missing_file_is_named(self):
        with self.assertRaises(SystemExit) as cm:
            star_curator.load_rules("/nonexistent/star-rules.yml")
        self.assertIn("not found", str(cm.exception))


class TestMainFilingLoop(unittest.TestCase):
    """Exercises main() itself. The in-memory bookkeeping bugs live here, not
    in the pure helpers, so testing build_report with hand-built args misses
    them entirely."""

    def _run(self, stars, lists, rules, file_side_effect=None, extra_args=()):
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(rules, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)

        captured = {}

        def fake_report(filed, ambiguous, rot, drift, dry_run, *rest):
            captured.update(
                filed=filed, ambiguous=ambiguous, rot=rot, drift=drift,
                dry_run=dry_run, rest=rest, lists=lists,
            )
            return "report"

        with mock.patch.dict(os.environ, {"STARS_TOKEN": "tok"}, clear=False), \
             mock.patch.object(star_curator, "assert_list_api"), \
             mock.patch.object(star_curator, "fetch_stars", return_value=stars), \
             mock.patch.object(star_curator, "fetch_lists", return_value=lists), \
             mock.patch.object(star_curator, "file_repo", side_effect=file_side_effect), \
             mock.patch.object(star_curator, "build_report", side_effect=fake_report), \
             mock.patch.object(
                 sys, "argv", ["star_curator", "--rules", fh.name, *extra_args]
             ):
            code = star_curator.main()
        return code, captured

    RULES = {"oversize": 2, "lists": {"HA": {"topics": ["hacs"]}}}

    def _lists(self):
        return {"HA": {"id": "L1", "items": {"seed/one", "seed/two"}}}

    def test_a_successful_write_updates_the_in_memory_view(self):
        stars = [repo(full_name="a/b", topics=["hacs"])]
        lists = self._lists()
        _, cap = self._run(stars, lists, self.RULES)
        self.assertIn("a/b", lists["HA"]["items"])
        # Three members now, over the oversize of 2 — reported in the same run.
        self.assertTrue(any("HA" in d for d in cap["drift"]))

    def test_a_failed_write_does_not_invent_membership(self):
        # The blocker. Two repos match; the first write fails, so NEITHER may
        # be recorded as a member — the second was never even attempted.
        stars = [
            repo(full_name="a/b", topics=["hacs"]),
            repo(full_name="c/d", topics=["hacs"]),
        ]
        lists = self._lists()
        boom = star_curator.ApiError("HTTP 502")
        code, cap = self._run(stars, lists, self.RULES, file_side_effect=boom)
        self.assertNotIn("a/b", lists["HA"]["items"])
        self.assertNotIn("c/d", lists["HA"]["items"])
        self.assertEqual(lists["HA"]["items"], {"seed/one", "seed/two"})
        self.assertEqual(code, 1)
        # Both still reported, so the run says what it intended to do.
        self.assertEqual([n for n, _, _ in cap["filed"]], ["a/b", "c/d"])
        # Neither was written: the first failed, the second was never tried.
        self.assertEqual([w for _, _, w in cap["filed"]], [False, False])

    def test_a_filing_failure_marks_the_run_as_having_findings(self):
        # has_findings gates the issue steps. A degraded or partly-failed run
        # with no rot or drift must still open a report, or the loudest
        # problem produces the quietest outcome.
        stars = [repo(full_name="a/b", topics=["hacs"])]
        lists = {"HA": {"id": "L1", "items": set()}}
        out_file = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "__pycache__", "gh_out"
        )
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        open(out_file, "w").close()
        self.addCleanup(os.unlink, out_file)
        boom = star_curator.ApiError("HTTP 502")
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out_file}, clear=False):
            code, _ = self._run(stars, lists, self.RULES, file_side_effect=boom)
        self.assertEqual(code, 1)
        self.assertIn("has_findings=true", open(out_file).read())

    def test_a_dry_run_invents_no_membership_either(self):
        stars = [repo(full_name="a/b", topics=["hacs"])]
        lists = self._lists()
        _, cap = self._run(stars, lists, self.RULES, extra_args=["--dry-run"])
        self.assertNotIn("a/b", lists["HA"]["items"])
        self.assertEqual(cap["drift"], [])


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
