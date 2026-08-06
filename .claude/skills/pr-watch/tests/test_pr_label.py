import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pr_label import (  # noqa: E402
    TAXONOMY,
    REVIEW_STATES,
    SHIP_STATES,
    TERMINAL_SHIP_STATES,
    USER_COURT_LABELS,
    FLAG_LABELS,
    UnknownStateError,
    plan_transition,
    verdict_from_findings,
    ensure,
    set_state,
    main,
)


class TestTaxonomy(unittest.TestCase):
    # Counts derive from TAXONOMY rather than being written out. These tests
    # assert ensure()'s *behaviour* — create the missing ones, skip the present
    # ones — and hardcoding the total made every one of them fail the moment a
    # label was legitimately added, which reads as five regressions instead of
    # one intentional change.
    ALL = len(TAXONOMY)

    def test_exact_label_set(self):
        # The one place the full vocabulary is written out: growing or
        # shrinking the taxonomy must be an intentional, reviewable edit here,
        # not an incidental count change.
        names = {name for name, _, _ in TAXONOMY}
        self.assertEqual(names, {
            "review:new", "review:re-review", "review:blocker", "review:nits",
            "review:approved", "review:inconclusive",
            "ship:ready", "ship:blocked", "ship:escalated", "ship:parked",
            "ship:deferred", "ship:superseded", "ship:probe",
            "claude-fix", "no-pr-watch", "claude-review",
        })

    def test_colors_are_unique(self):
        colors = [color for _, color, _ in TAXONOMY]
        self.assertEqual(len(colors), len(set(colors)))

    def test_claude_review_is_present(self):
        names = [name for name, _, _ in TAXONOMY]
        self.assertIn("claude-review", names)

    def test_review_states_are_a_subset_of_taxonomy(self):
        names = {name for name, _, _ in TAXONOMY}
        self.assertTrue(set(REVIEW_STATES).issubset(names))

    def test_ship_states_are_a_subset_of_taxonomy(self):
        names = {name for name, _, _ in TAXONOMY}
        self.assertTrue(set(SHIP_STATES).issubset(names))

    def test_axes_partition_the_taxonomy(self):
        # Every label belongs to exactly one axis (R, S, or flags); the axes
        # never overlap and together cover the whole taxonomy.
        names = {name for name, _, _ in TAXONOMY}
        r, s, f = set(REVIEW_STATES), set(SHIP_STATES), set(FLAG_LABELS)
        self.assertEqual(r | s | f, names)
        self.assertFalse(r & s)
        self.assertFalse(r & f)
        self.assertFalse(s & f)

    def test_inconclusive_is_a_review_state(self):
        self.assertIn("review:inconclusive", REVIEW_STATES)

    def test_ship_blocked_is_not_a_review_state(self):
        # It is written by the shipping side, not by a reviewer verdict, so it
        # must not participate in the exactly-one-review-label invariant.
        self.assertNotIn("ship:blocked", REVIEW_STATES)

    def test_terminal_ship_states_exclude_the_waiting_pair(self):
        # ship:ready is terminal-until-revoked and ship:blocked is a
        # non-terminal waiting state; only the other five are hard terminals.
        self.assertNotIn("ship:ready", TERMINAL_SHIP_STATES)
        self.assertNotIn("ship:blocked", TERMINAL_SHIP_STATES)
        self.assertTrue(set(TERMINAL_SHIP_STATES).issubset(set(SHIP_STATES)))

    def test_user_court_is_queryable_as_one_set(self):
        # The human's single query: everything waiting on them.
        self.assertEqual(
            set(USER_COURT_LABELS),
            {"ship:ready", "ship:blocked", "ship:escalated"})
        self.assertTrue(set(USER_COURT_LABELS).issubset(set(SHIP_STATES)))


class TestPlanTransition(unittest.TestCase):
    def test_adds_target_when_no_labels(self):
        add, remove = plan_transition([], "review:approved")
        self.assertEqual(add, ["review:approved"])
        self.assertEqual(remove, [])

    def test_swaps_stale_verdict(self):
        add, remove = plan_transition(["review:approved"], "review:blocker")
        self.assertEqual(add, ["review:blocker"])
        self.assertEqual(remove, ["review:approved"])

    def test_blocker_clears_ship_ready(self):
        add, remove = plan_transition(
            ["review:approved", "ship:ready"], "review:blocker")
        self.assertEqual(add, ["review:blocker"])
        self.assertIn("ship:ready", remove)
        self.assertIn("review:approved", remove)

    def test_approved_preserves_ship_ready(self):
        add, remove = plan_transition(["ship:ready"], "review:approved")
        self.assertEqual(add, ["review:approved"])
        self.assertEqual(remove, [])

    def test_already_correct_is_a_noop(self):
        add, remove = plan_transition(["review:blocker"], "review:blocker")
        self.assertEqual(add, [])
        self.assertEqual(remove, [])

    def test_blocker_already_set_still_clears_ship_ready(self):
        add, remove = plan_transition(
            ["review:blocker", "ship:ready"], "review:blocker")
        self.assertEqual(add, [])
        self.assertEqual(remove, ["ship:ready"])

    def test_unrelated_labels_untouched(self):
        add, remove = plan_transition(
            ["priority: high", "claude-fix", "review:new"], "review:nits")
        self.assertEqual(add, ["review:nits"])
        self.assertEqual(remove, ["review:new"])

    def test_ship_blocked_untouched_by_review_transitions(self):
        # ship:blocked coexists with any review verdict (the canonical gated
        # state is review:blocker + ship:blocked); a verdict swap must not
        # disturb it.
        add, remove = plan_transition(
            ["review:re-review", "ship:blocked"], "review:blocker")
        self.assertEqual(add, ["review:blocker"])
        self.assertEqual(remove, ["review:re-review"])

    def test_inconclusive_is_a_valid_target(self):
        add, remove = plan_transition(["review:re-review"], "review:inconclusive")
        self.assertEqual(add, ["review:inconclusive"])
        self.assertEqual(remove, ["review:re-review"])

    def test_inconclusive_preserves_ship_ready(self):
        # Inconclusive is "no verdict", not "changes requested" — it must not
        # revoke an existing clearance the way a blocker does.
        add, remove = plan_transition(["ship:ready"], "review:inconclusive")
        self.assertEqual(add, ["review:inconclusive"])
        self.assertEqual(remove, [])

    def test_noncanonical_review_label_removed(self):
        # A hand-made review:wip (or a legacy name not yet migrated) is not
        # in REVIEW_STATES, but the "exactly one review:* label" invariant
        # still requires it to go — plan_transition must not leave it behind
        # just because it isn't one of the canonical names.
        add, remove = plan_transition(
            ["review:wip", "priority: high"], "review:approved")
        self.assertEqual(add, ["review:approved"])
        self.assertEqual(remove, ["review:wip"])

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            plan_transition([], "review:aproved")

    def test_unknown_target_raises_unknown_state_error(self):
        with self.assertRaises(UnknownStateError):
            plan_transition([], "review:aproved")

    def test_ship_states_are_not_valid_targets(self):
        # Shipping disposition is not a review verdict: `set ship:ready` must
        # exit 2, and the underlying planner must refuse every ship:* state.
        for state in SHIP_STATES:
            with self.assertRaises(UnknownStateError):
                plan_transition([], state)


class TestVerdictFromFindings(unittest.TestCase):
    def test_no_findings_is_approved(self):
        self.assertEqual(verdict_from_findings([]), "review:approved")

    def test_high_is_blocker(self):
        self.assertEqual(verdict_from_findings(["High"]), "review:blocker")

    def test_medium_is_blocker(self):
        self.assertEqual(verdict_from_findings(["Medium"]), "review:blocker")

    def test_low_only_is_nits(self):
        self.assertEqual(verdict_from_findings(["Low"]), "review:nits")

    def test_nit_only_is_nits(self):
        self.assertEqual(verdict_from_findings(["Nit", "Nit"]), "review:nits")

    def test_mixed_takes_the_worst(self):
        self.assertEqual(
            verdict_from_findings(["Nit", "Low", "High"]), "review:blocker")

    def test_case_insensitive(self):
        self.assertEqual(verdict_from_findings(["hIgH"]), "review:blocker")

    def test_blank_entries_ignored(self):
        self.assertEqual(verdict_from_findings(["", "  "]), "review:approved")

    def test_unknown_severity_raises(self):
        with self.assertRaises(ValueError):
            verdict_from_findings(["Critical"])

    def test_unknown_severity_raises_unknown_state_error(self):
        with self.assertRaises(UnknownStateError):
            verdict_from_findings(["Critical"])


class FakeRunner:
    """Records gh argv lists instead of executing them.

    Returns valid JSON for `gh label list` calls — `existing_labels`,
    defaulting to none — so ensure()'s existing-label read has real data to
    parse instead of an empty string. Everything else (creates, edits)
    returns "", since no caller parses those return values.
    """

    def __init__(self, existing_labels=()):
        self.calls = []
        self._existing_labels = list(existing_labels)

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": name} for name in self._existing_labels])
        return ""


class TestEnsure(unittest.TestCase):
    ALL = len(TAXONOMY)

    def test_creates_every_label_when_repo_is_empty(self):
        run = FakeRunner()
        created = ensure("o/r", existing=set(), run=run)
        self.assertEqual(len(created), self.ALL)
        self.assertEqual(len(run.calls), self.ALL)
        self.assertIn("--force", run.calls[0])

    def test_skips_labels_that_already_exist(self):
        run = FakeRunner()
        present = {"review:new", "ship:ready"}
        created = ensure("o/r", existing=present, run=run)
        self.assertEqual(len(created), self.ALL - len(present))
        self.assertEqual(len(run.calls), self.ALL - len(present))
        self.assertNotIn("review:new", created)

    def test_reconcile_upserts_everything(self):
        run = FakeRunner()
        created = ensure("o/r", existing={"review:new"}, run=run, reconcile=True)
        self.assertEqual(len(created), self.ALL)
        self.assertEqual(len(run.calls), self.ALL)

    def test_dry_run_reports_without_calling(self):
        # `existing` is supplied, so ensure() must never reach _gh at all —
        # proven by making _gh raise if touched, not by a FakeRunner that
        # was never wired into the call (which would pass vacuously).
        with mock.patch("pr_label._gh",
                        side_effect=AssertionError("dry run must not shell out")):
            created = ensure("o/r", existing=set(), run=None)
        self.assertEqual(len(created), self.ALL)

    def test_dry_run_reflects_fetched_existing_labels(self):
        # A dry run with no `existing` given must still consult real state
        # (here, fetch_existing standing in for a real `gh label list` call)
        # rather than assuming the repo is empty and reporting everything.
        present = {"review:new", "ship:ready"}
        created = ensure(
            "o/r", run=None,
            fetch_existing=lambda repo: set(present))
        self.assertEqual(len(created), self.ALL - len(present))
        self.assertNotIn("review:new", created)
        self.assertNotIn("ship:ready", created)


class TestSetState(unittest.TestCase):
    def test_adds_and_removes(self):
        # Repo already carries the full taxonomy, so this exercises the
        # documented steady state: one `label list` read, zero `label
        # create` writes, then the actual `pr edit`.
        run = FakeRunner(existing_labels=[name for name, _, _ in TAXONOMY])
        add, remove = set_state(
            "o/r", 7, "review:blocker",
            fetch_labels=lambda repo, pr: ["review:approved", "ship:ready"],
            run=run)
        self.assertEqual(add, ["review:blocker"])
        self.assertCountEqual(remove, ["review:approved", "ship:ready"])
        list_calls = [c for c in run.calls if c[:2] == ["label", "list"]]
        create_calls = [c for c in run.calls if c[:2] == ["label", "create"]]
        edit_calls = [c for c in run.calls if c[:2] == ["pr", "edit"]]
        self.assertEqual(len(list_calls), 1)
        self.assertEqual(create_calls, [])
        self.assertTrue(edit_calls)
        flat = " ".join(" ".join(c) for c in edit_calls)
        self.assertIn("--add-label", flat)
        self.assertIn("--remove-label", flat)

    def test_noop_issues_no_edit(self):
        run = FakeRunner()
        add, remove = set_state(
            "o/r", 7, "review:nits",
            fetch_labels=lambda repo, pr: ["review:nits"],
            run=run)
        self.assertEqual((add, remove), ([], []))
        self.assertEqual([c for c in run.calls if c[:2] == ["pr", "edit"]], [])

    def test_unknown_state_raises_before_any_call(self):
        run = FakeRunner()
        with self.assertRaises(ValueError):
            set_state("o/r", 7, "blocker",
                      fetch_labels=lambda repo, pr: [], run=run)
        self.assertEqual(run.calls, [])

    def test_ship_ready_raises_before_any_call(self):
        # The documented contract: `pr_label.py set <repo> <pr> ship:ready`
        # exits 2. ship:* labels are applied with `gh pr edit` after `ensure`.
        run = FakeRunner()
        with self.assertRaises(UnknownStateError):
            set_state("o/r", 7, "ship:ready",
                      fetch_labels=lambda repo, pr: [], run=run)
        self.assertEqual(run.calls, [])

    def test_dry_run_returns_the_real_plan_without_writing(self):
        # run=None must still consult current labels, so the printed plan is
        # what would actually happen — not a hypothetical from an empty set.
        add, remove = set_state(
            "o/r", 7, "review:blocker",
            fetch_labels=lambda repo, pr: ["review:approved", "ship:ready"],
            run=None)
        self.assertEqual(add, ["review:blocker"])
        self.assertCountEqual(remove, ["review:approved", "ship:ready"])


class TestCli(unittest.TestCase):
    def test_set_rejects_unknown_state_before_touching_the_network(self):
        # set_state validates the state before calling fetch, so this is safe
        # to assert without stubbing gh. Errors go to stderr; capture it so
        # the test suite's own output stays pristine.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["set", "o/r", "7", "blocker", "--dry-run"]), 2)

    def test_set_ship_ready_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["set", "o/r", "7", "ship:ready", "--dry-run"]), 2)

    def test_ensure_dry_run_succeeds(self):
        # ensure --dry-run now genuinely reads existing labels (see
        # TestEnsure.test_dry_run_reflects_fetched_existing_labels), so this
        # CLI-level call must stub the true network boundary (_gh) rather
        # than a fake repo hitting a real `gh` call. Also capture stdout —
        # main() prints its result, and the suite's own output must stay
        # pristine.
        with mock.patch("pr_label._gh", return_value="[]"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["ensure", "o/r", "--dry-run"]), 0)

    def test_set_from_severities_computes_the_state(self):
        # --from-severities routes through verdict_from_findings rather than
        # taking a hand-picked label — High present means review:blocker.
        with mock.patch("pr_label._gh", return_value='{"labels": []}'), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            rc = main(["set", "o/r", "7", "--from-severities", "Low,High",
                       "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("review:blocker", out.getvalue())

    def test_set_from_severities_unknown_severity_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = main(["set", "o/r", "7", "--from-severities", "Critical",
                       "--dry-run"])
        self.assertEqual(rc, 2)

    def test_set_state_and_from_severities_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            main(["set", "o/r", "7", "review:blocker",
                  "--from-severities", "High", "--dry-run"])

    def test_set_requires_state_or_from_severities(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            main(["set", "o/r", "7", "--dry-run"])

    def test_malformed_gh_response_exits_one_not_two(self):
        # json.JSONDecodeError is a ValueError subclass. If main() caught
        # ValueError before UnknownStateError (or didn't distinguish them),
        # a real gh I/O failure would be misreported as exit 2 ("unknown
        # state") instead of exit 1 ("any other failure") — see reviewer.md's
        # documented exit-code contract.
        with mock.patch("pr_label._gh", return_value="not json"), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = main(["set", "o/r", "7", "review:blocker", "--dry-run"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
