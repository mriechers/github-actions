import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from ship_scan import (  # noqa: E402
    agent_name,
    classify,
    enrich,
    gate_mode,
    group_by_repo,
    is_opted_out,
    label_names,
    local_checkouts,
    origin_url,
    parse_remote_nwo,
    scan,
    summarize,
    verdict_of,
)


def pr(number=1, repo="mriechers/demo", labels=(), draft=False, **extra):
    """A gh-search-shaped PR record."""
    record = {
        "repository": {"nameWithOwner": repo},
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "labels": [{"name": name} for name in labels],
        "isDraft": draft,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-04T00:00:00Z",
    }
    record.update(extra)
    return record


class TestLabelHelpers(unittest.TestCase):
    def test_label_names(self):
        self.assertEqual(label_names(pr(labels=["review:nits", "bug"])),
                         {"review:nits", "bug"})

    def test_label_names_missing_key(self):
        self.assertEqual(label_names({}), set())

    def test_label_names_tolerates_null_name(self):
        self.assertEqual(label_names({"labels": [{"name": None}]}), {""})

    def test_opted_out(self):
        self.assertTrue(is_opted_out(pr(labels=["no-pr-watch"])))
        self.assertFalse(is_opted_out(pr(labels=["review:nits"])))

    def test_opted_out_is_case_insensitive(self):
        self.assertTrue(is_opted_out(pr(labels=["No-PR-Watch"])))

    def test_verdict_blocker_beats_nits(self):
        self.assertEqual(verdict_of(pr(labels=["review:nits", "review:blocker"])),
                         "review:blocker")

    def test_verdict_none_when_unlabeled(self):
        self.assertIsNone(verdict_of(pr()))


class TestClassify(unittest.TestCase):
    def test_assign_on_blocker(self):
        self.assertEqual(classify(pr(labels=["review:blocker"])), "assign")

    def test_assign_on_nits(self):
        self.assertEqual(classify(pr(labels=["review:nits"])), "assign")

    def test_in_flight_on_re_review(self):
        self.assertEqual(classify(pr(labels=["review:re-review"])), "in_flight")

    def test_clear_on_approved(self):
        self.assertEqual(classify(pr(labels=["review:approved"])), "clear")

    def test_done_on_ship_ready(self):
        self.assertEqual(classify(pr(labels=["ship:ready"])), "done")

    def test_ship_ready_beats_a_stale_verdict(self):
        # Terminal state wins: a cleared PR is never re-dispatched, even if a
        # stale verdict label is still hanging around next to it.
        self.assertEqual(classify(pr(labels=["ship:ready", "review:nits"])), "done")

    def test_skip_draft_even_with_verdict(self):
        self.assertEqual(classify(pr(labels=["review:blocker"], draft=True)), "skip")

    def test_skip_opted_out_even_with_verdict(self):
        self.assertEqual(classify(pr(labels=["review:nits", "no-pr-watch"])), "skip")

    def test_skip_unlabeled(self):
        # No reviewer verdict yet — that is /pr-watch's backlog, not ours.
        self.assertEqual(classify(pr()), "skip")

    def test_skip_review_new(self):
        self.assertEqual(classify(pr(labels=["review:new"])), "skip")

    def test_awaiting_user_on_ship_blocked(self):
        self.assertEqual(classify(pr(labels=["ship:blocked"])), "awaiting_user")

    def test_ship_blocked_beats_assign(self):
        # The gated PR keeps its review:blocker verdict while it waits. Without
        # this precedence the Leader would re-dispatch and re-notify it every
        # tick for as long as the user took to answer.
        self.assertEqual(
            classify(pr(labels=["review:blocker", "ship:blocked"])), "awaiting_user")

    def test_ship_ready_beats_ship_blocked(self):
        self.assertEqual(
            classify(pr(labels=["ship:blocked", "ship:ready"])), "done")


class TestGateMode(unittest.TestCase):
    def test_blocker_is_gated_by_default(self):
        self.assertEqual(gate_mode("review:blocker"), "gated")

    def test_nits_is_auto(self):
        self.assertEqual(gate_mode("review:nits"), "auto")

    def test_auto_blockers_flips_the_gate(self):
        self.assertEqual(gate_mode("review:blocker", auto_blockers=True), "auto")


class TestAgentName(unittest.TestCase):
    def test_slash_free(self):
        self.assertEqual(agent_name("mriechers/the-lodge"), "sw-mriechers-the-lodge")

    def test_every_non_alnum_becomes_a_dash(self):
        self.assertEqual(agent_name("Wonder-Cabinet-Productions/wc.audio"),
                         "sw-Wonder-Cabinet-Productions-wc-audio")

    def test_name_has_no_slash(self):
        self.assertNotIn("/", agent_name("public-media-work/pbswi"))


class TestRemoteParsing(unittest.TestCase):
    def test_scp_style_ssh(self):
        self.assertEqual(parse_remote_nwo("git@github.com:mriechers/the-lodge.git"),
                         "mriechers/the-lodge")

    def test_https(self):
        self.assertEqual(parse_remote_nwo("https://github.com/mriechers/crows-nest.git"),
                         "mriechers/crows-nest")

    def test_https_without_suffix(self):
        self.assertEqual(parse_remote_nwo("https://github.com/mriechers/crows-nest"),
                         "mriechers/crows-nest")

    def test_ssh_scheme(self):
        self.assertEqual(parse_remote_nwo("ssh://git@github.com/owner/repo.git"),
                         "owner/repo")

    def test_rejects_garbage(self):
        self.assertIsNone(parse_remote_nwo("not-a-url"))
        self.assertIsNone(parse_remote_nwo(""))
        self.assertIsNone(parse_remote_nwo(None))


class TestOriginUrl(unittest.TestCase):
    CONFIG = """
[core]
\trepositoryformatversion = 0
[remote "upstream"]
\turl = git@github.com:someone/else.git
\tfetch = +refs/heads/*:refs/remotes/upstream/*
[remote "origin"]
\turl = git@github.com:mriechers/the-lodge.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
\tfetch = +refs/pull/*/head:refs/remotes/origin/pr/*
[branch "main"]
\tremote = origin
"""

    def test_picks_origin_not_upstream(self):
        self.assertEqual(origin_url(self.CONFIG),
                         "git@github.com:mriechers/the-lodge.git")

    def test_tolerates_duplicate_fetch_keys(self):
        # configparser would reject these under strict parsing; we must not.
        self.assertIsNotNone(origin_url(self.CONFIG))

    def test_none_when_no_origin(self):
        self.assertIsNone(origin_url('[core]\n\tbare = false\n'))

    def test_none_on_empty(self):
        self.assertIsNone(origin_url(""))


class TestLocalCheckouts(unittest.TestCase):
    def _make_repo(self, root, name, url, as_dir=True):
        path = os.path.join(root, name)
        os.makedirs(path, exist_ok=True)
        config = f'[remote "origin"]\n\turl = {url}\n'
        if as_dir:
            os.makedirs(os.path.join(path, ".git"), exist_ok=True)
            with open(os.path.join(path, ".git", "config"), "w") as handle:
                handle.write(config)
        else:
            gitdir = os.path.join(root, f".{name}-gitdir")
            os.makedirs(gitdir, exist_ok=True)
            with open(os.path.join(gitdir, "config"), "w") as handle:
                handle.write(config)
            with open(os.path.join(path, ".git"), "w") as handle:
                handle.write(f"gitdir: {gitdir}\n")
        return path

    def test_finds_repo_at_depth_one(self):
        with tempfile.TemporaryDirectory() as root:
            want = self._make_repo(root, "the-lodge",
                                   "git@github.com:mriechers/the-lodge.git")
            found = local_checkouts([root])
            self.assertEqual(found.get("mriechers/the-lodge"), want)

    def test_finds_repo_nested_in_a_metarepo(self):
        with tempfile.TemporaryDirectory() as root:
            meta = os.path.join(root, "homelab")
            os.makedirs(meta)
            want = self._make_repo(meta, "opnsense-config",
                                   "git@github.com:mriechers/opnsense-config.git")
            found = local_checkouts([root])
            self.assertEqual(found.get("mriechers/opnsense-config"), want)

    def test_skips_worktrees(self):
        with tempfile.TemporaryDirectory() as root:
            wt = os.path.join(root, "repo", ".worktrees")
            os.makedirs(wt)
            self._make_repo(wt, "feature-x", "git@github.com:mriechers/wt-repo.git")
            self.assertNotIn("mriechers/wt-repo", local_checkouts([root]))

    def test_real_git_dir_wins_over_gitdir_file(self):
        with tempfile.TemporaryDirectory() as root:
            url = "git@github.com:mriechers/dup.git"
            self._make_repo(root, "a-linked", url, as_dir=False)
            real = self._make_repo(root, "z-real", url, as_dir=True)
            self.assertEqual(local_checkouts([root]).get("mriechers/dup"), real)

    def test_missing_root_is_not_fatal(self):
        self.assertEqual(local_checkouts(["/nonexistent/path/xyz"]), {})


class TestEnrichAndGroup(unittest.TestCase):
    def test_enrich_carries_verdict_and_path(self):
        rec = enrich(pr(number=7, labels=["review:nits"]), local_path="/tmp/demo")
        self.assertEqual(rec["action"], "assign")
        self.assertEqual(rec["verdict"], "review:nits")
        self.assertEqual(rec["local_path"], "/tmp/demo")
        self.assertEqual(rec["number"], 7)

    def test_group_buckets_by_action(self):
        records = [
            enrich(pr(number=1, labels=["review:nits"])),
            enrich(pr(number=2, labels=["review:approved"])),
            enrich(pr(number=3, labels=["ship:ready"])),
            enrich(pr(number=4, labels=["review:re-review"])),
        ]
        [entry] = group_by_repo(records)
        self.assertEqual([p["number"] for p in entry["assign"]], [1])
        self.assertEqual([p["number"] for p in entry["clear"]], [2])
        self.assertEqual([p["number"] for p in entry["done"]], [3])
        self.assertEqual([p["number"] for p in entry["in_flight"]], [4])
        self.assertEqual(entry["actionable"], 2)

    def test_group_sets_mode_on_assign_only(self):
        records = [
            enrich(pr(number=1, labels=["review:blocker"])),
            enrich(pr(number=2, labels=["review:approved"])),
        ]
        [entry] = group_by_repo(records)
        self.assertEqual(entry["assign"][0]["mode"], "gated")
        self.assertNotIn("mode", entry["clear"][0])

    def test_group_orders_by_actionable_count_descending(self):
        records = [
            enrich(pr(number=1, repo="o/small", labels=["review:nits"])),
            enrich(pr(number=2, repo="o/big", labels=["review:nits"])),
            enrich(pr(number=3, repo="o/big", labels=["review:nits"])),
        ]
        self.assertEqual([e["repo"] for e in group_by_repo(records)],
                         ["o/big", "o/small"])

    def test_needs_clone_when_no_local_path(self):
        [entry] = group_by_repo([enrich(pr(labels=["review:nits"]))])
        self.assertTrue(entry["needs_clone"])
        self.assertEqual(entry["clone_url"], "https://github.com/mriechers/demo.git")

    def test_no_clone_needed_when_nothing_actionable(self):
        # A repo whose only PRs are done or skipped needs no working copy.
        [entry] = group_by_repo([enrich(pr(labels=["ship:ready"]))])
        self.assertFalse(entry["needs_clone"])

    def test_local_path_propagates_from_any_pr_in_the_repo(self):
        records = [
            enrich(pr(number=1, labels=["review:nits"]), local_path=None),
            enrich(pr(number=2, labels=["review:nits"]), local_path="/tmp/demo"),
        ]
        [entry] = group_by_repo(records)
        self.assertEqual(entry["local_path"], "/tmp/demo")
        self.assertFalse(entry["needs_clone"])


class TestScan(unittest.TestCase):
    PRS = [
        pr(number=1, repo="o/alpha", labels=["review:nits"]),
        pr(number=2, repo="o/alpha", labels=["review:blocker"]),
        pr(number=3, repo="o/beta", labels=["ship:ready"]),
        pr(number=4, repo="o/gamma", labels=[]),
    ]

    def test_summary_counts(self):
        result = scan(self.PRS)
        summary = result["summary"]
        self.assertEqual(summary["prs_to_assign"], 2)
        self.assertEqual(summary["prs_done"], 1)
        self.assertEqual(summary["gated"], 1)
        self.assertEqual(summary["agents_required"], 1)
        self.assertEqual(summary["repos_total"], 3)

    def test_actionable_only_filters_repos(self):
        result = scan(self.PRS, actionable_only=True)
        self.assertEqual([r["repo"] for r in result["repos"]], ["o/alpha"])

    def test_summary_is_computed_before_filtering(self):
        # The summary must describe the whole workspace even when the emitted
        # repo list is trimmed, or --actionable-only would silently under-report.
        self.assertEqual(scan(self.PRS, actionable_only=True)["summary"]["repos_total"], 3)

    def test_repo_filter_scopes_the_scan(self):
        result = scan(self.PRS, repo="o/alpha")
        self.assertEqual([r["repo"] for r in result["repos"]], ["o/alpha"])
        self.assertEqual(result["summary"]["prs_to_assign"], 2)

    def test_auto_blockers_propagates(self):
        result = scan(self.PRS, auto_blockers=True)
        self.assertEqual(result["summary"]["gated"], 0)

    def test_checkouts_are_attached(self):
        result = scan(self.PRS, checkouts={"o/alpha": "/tmp/alpha"})
        alpha = next(r for r in result["repos"] if r["repo"] == "o/alpha")
        self.assertEqual(alpha["local_path"], "/tmp/alpha")
        self.assertFalse(alpha["needs_clone"])

    def test_needs_clone_listed_in_summary(self):
        self.assertEqual(scan(self.PRS)["summary"]["needs_clone"], ["o/alpha"])

    def test_empty_scan(self):
        result = scan([])
        self.assertEqual(result["repos"], [])
        self.assertEqual(result["summary"]["agents_required"], 0)

    def test_in_your_court_sums_all_human_queues(self):
        # This is the number that answers "where do I look" — it must equal
        # what `label:ship:ready,ship:blocked,ship:escalated` returns on GitHub.
        summary = scan([
            pr(number=1, repo="o/a", labels=["ship:ready"]),
            pr(number=2, repo="o/a", labels=["ship:blocked", "review:blocker"]),
            pr(number=3, repo="o/b", labels=["review:nits"]),
            pr(number=4, repo="o/b", labels=["ship:escalated", "review:blocker"]),
        ])["summary"]
        self.assertEqual(summary["prs_done"], 1)
        self.assertEqual(summary["prs_awaiting_user"], 2)
        self.assertEqual(summary["in_your_court"], 3)

    def test_gated_pr_awaiting_user_is_not_reassigned(self):
        result = scan([pr(number=1, repo="o/a",
                          labels=["review:blocker", "ship:blocked"])])
        self.assertEqual(result["summary"]["prs_to_assign"], 0)
        self.assertEqual(result["summary"]["agents_required"], 0)

    def test_escalated_pr_is_not_reassigned(self):
        # Escalated means the loop already stopped on this PR; a human must
        # unstick it. Re-dispatching would recreate the loop that escalated.
        result = scan([pr(number=1, repo="o/a",
                          labels=["review:blocker", "ship:escalated"])])
        self.assertEqual(result["summary"]["prs_to_assign"], 0)
        self.assertEqual(result["summary"]["prs_awaiting_user"], 1)

    def test_terminal_dispositions_are_never_redispatched(self):
        summary = scan([
            pr(number=1, repo="o/a", labels=["ship:superseded", "review:blocker"]),
            pr(number=2, repo="o/a", labels=["ship:probe", "review:nits"]),
            pr(number=3, repo="o/a", labels=["ship:parked"]),
            pr(number=4, repo="o/a", labels=["ship:deferred"]),
        ])["summary"]
        self.assertEqual(summary["prs_terminal"], 4)
        self.assertEqual(summary["prs_to_assign"], 0)
        self.assertEqual(summary["in_your_court"], 0)
        self.assertEqual(summary["agents_required"], 0)


class TestSummarize(unittest.TestCase):
    def test_counts_only_actionable_repos_as_agents(self):
        repos = group_by_repo([
            enrich(pr(number=1, repo="o/a", labels=["review:nits"])),
            enrich(pr(number=2, repo="o/b", labels=["ship:ready"])),
        ])
        self.assertEqual(summarize(repos)["agents_required"], 1)

    def test_clear_only_repo_needs_no_clone_and_no_agent(self):
        # The Leader clears review:approved PRs inline over the gh API — no
        # checkout, no shipwright. A clear-only repo with no local path must
        # not appear in needs_clone or count toward agents_required.
        repos = group_by_repo([
            enrich(pr(number=1, repo="o/clearonly", labels=["review:approved"])),
        ])
        self.assertEqual(repos[0]["needs_clone"], False)
        summary = summarize(repos)
        self.assertEqual(summary["needs_clone"], [])
        self.assertEqual(summary["agents_required"], 0)
        self.assertEqual(summary["prs_to_clear"], 1)

    def test_assign_repo_without_checkout_still_needs_clone(self):
        repos = group_by_repo([
            enrich(pr(number=1, repo="o/fixme", labels=["review:blocker"])),
        ])
        self.assertEqual(repos[0]["needs_clone"], True)
        self.assertEqual(summarize(repos)["agents_required"], 1)


if __name__ == "__main__":
    unittest.main()
