"""The 'Resolve the ref under review' step in claude-interactive.yml.

The step decides what code the reviewer reads, so it is worth real coverage rather
than a re-derivation: this loads the step's `run:` block straight out of the
workflow and executes it against a stub `gh`, so the test cannot drift from what
CI actually runs.

Run: python3 -m unittest tests.test_resolve_review_ref
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-interactive.yml"
STEP_NAME = "Resolve the ref under review"


def _step_script() -> str:
    """The step's shell body, pulled from the workflow itself."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        raise unittest.SkipTest("pyyaml not available")
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["claude"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"step {STEP_NAME!r} not found in {WORKFLOW}")


class ResolveReviewRefTest(unittest.TestCase):
    """Each case runs the real step body with a fake `gh` on PATH."""

    def run_step(self, pr_number: str, pr_json: dict | None = None, gh_exit: int = 0):
        """Return (exit_code, outputs dict, stdout+stderr)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            # Stub gh: ignores its args, prints the canned PR payload. gh_exit
            # simulates a transient API failure (rate limit, network blip).
            (bindir / "gh").write_text(
                "#!/bin/sh\ncat <<'JSON'\n"
                + json.dumps(pr_json or {})
                + f"\nJSON\nexit {gh_exit}\n",
                encoding="utf-8",
            )
            (bindir / "gh").chmod(0o755)

            out_file = tmp / "github_output"
            out_file.touch()
            env = {
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "GITHUB_OUTPUT": str(out_file),
                "PR_NUMBER": pr_number,
                "REPO": "owner/repo",
                "GH_TOKEN": "stub",
            }
            proc = subprocess.run(
                ["bash", "-c", _step_script()],
                env=env, capture_output=True, text=True,
            )
            outputs = dict(
                line.split("=", 1)
                for line in out_file.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            return proc.returncode, outputs, proc.stdout + proc.stderr

    def _pr(self, head_repo: str, sha: str = "a" * 40) -> dict:
        return {
            "head": {"sha": sha, "repo": {"full_name": head_repo}},
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    def test_same_repo_pr_checks_out_the_head_sha(self):
        """The bug this step exists to fix: the reviewer must read the PR."""
        code, out, _ = self.run_step("621", self._pr("owner/repo", "b" * 40))
        self.assertEqual(code, 0)
        self.assertEqual(out["ref"], "b" * 40)

    def test_fork_pr_stays_on_the_default_branch(self):
        """refs/pull/N/head is untrusted code and this job can write + holds a token."""
        code, out, log = self.run_step("621", self._pr("attacker/repo"))
        self.assertEqual(code, 0)
        self.assertEqual(out["ref"], "", "checked out fork code")
        self.assertIn("Fork PR", log)

    def test_non_pr_event_resolves_to_the_default_ref(self):
        """`issues` fires with no PR head to resolve."""
        code, out, _ = self.run_step("")
        self.assertEqual(code, 0)
        self.assertEqual(out["ref"], "")

    def test_non_numeric_pr_number_is_refused(self):
        """PR_NUMBER selects what code runs — nothing but digits may reach it."""
        for hostile in ("12; rm -rf /", "../../main", "1 2", "$(id)"):
            with self.subTest(hostile=hostile):
                code, _, _ = self.run_step(hostile, self._pr("owner/repo"))
                self.assertEqual(code, 1, f"accepted {hostile!r}")

    def test_unresolvable_head_sha_falls_back_rather_than_guessing(self):
        code, out, log = self.run_step("621", self._pr("owner/repo", sha=""))
        self.assertEqual(code, 0)
        self.assertEqual(out["ref"], "")
        self.assertIn("Could not resolve", log)

    def test_api_failure_fails_closed_and_says_so(self):
        """A dead API call must not fall back to the default branch.

        Falling back there is precisely the bug this step fixes, and it would do
        it silently. Fail the job with an annotation instead.
        """
        code, _, log = self.run_step("621", gh_exit=1)
        self.assertEqual(code, 1)
        self.assertIn("::error::", log)
        self.assertIn("unidentified tree", log)


if __name__ == "__main__":
    unittest.main()
