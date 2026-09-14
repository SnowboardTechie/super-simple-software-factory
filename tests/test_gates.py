"""Gates compare the envelope's claims against the target repo, both ways.

`diff_matches_claims` asked only "does each claimed path exist on disk?" — so
claiming `README.md` passed in every repository that has one, and a file the
agent actually rewrote but never mentioned was invisible. A changed-file claim
is only worth gating if an omission fails it too.
"""

from __future__ import annotations

import unittest

from support import GitFixture, envelope

from adw_modules import gates, permissions


class ChangedFilesMatchTest(GitFixture):

    def baseline(self):
        run = self.run_for()
        run.tree_baseline = permissions.snapshot(run)
        return run

    def test_an_exact_claim_passes(self):
        run = self.baseline()
        self.write("src/app.py", "built\n")
        report = gates.changed_files_match(envelope(changed_files=["src/app.py"]), run)
        self.assertTrue(report.passed, report.violations)

    def test_an_omitted_path_fails(self):
        run = self.baseline()
        self.write("src/app.py", "built\n")
        self.write("src/quietly_also.py", "unmentioned\n")

        report = gates.changed_files_match(envelope(changed_files=["src/app.py"]), run)

        self.assertFalse(report.passed)
        self.assertIn("src/quietly_also.py", " ".join(report.violations))

    def test_an_invented_path_fails(self):
        run = self.baseline()
        self.write("src/app.py", "built\n")

        report = gates.changed_files_match(
            envelope(changed_files=["src/app.py", "src/never_touched.py"]), run)

        self.assertFalse(report.passed)
        self.assertIn("src/never_touched.py", " ".join(report.violations))

    def test_an_absolute_claim_inside_the_worktree_is_normalised(self):
        run = self.baseline()
        self.write("src/app.py", "built\n")
        report = gates.changed_files_match(
            envelope(changed_files=[str(self.root / "src/app.py")]), run)
        self.assertTrue(report.passed, report.violations)

    def test_a_claim_outside_the_worktree_fails(self):
        run = self.baseline()
        report = gates.changed_files_match(envelope(changed_files=["/etc/hosts"]), run)
        self.assertFalse(report.passed)

    def test_the_old_one_directional_gate_is_gone(self):
        self.assertFalse(hasattr(gates, "diff_matches_claims"))


class ArtifactGateTest(GitFixture):

    def test_an_empty_artifact_list_fails(self):
        """A gate that verifies nothing is not a pass."""
        report = gates.artifacts_exist(envelope(artifacts=[]), self.run_for())
        self.assertFalse(report.passed)

    def test_an_artifact_outside_the_allowed_roots_fails(self):
        run = self.run_for()
        outside = self.root.parent / "escaped.md"
        outside.write_text("elsewhere\n")
        self.addCleanup(outside.unlink)

        report = gates.artifacts_exist(envelope(artifacts=[str(outside)]), run)

        self.assertFalse(report.passed)
        self.assertIn("outside", " ".join(report.violations).lower())

    def test_a_missing_artifact_inside_the_roots_fails(self):
        report = gates.artifacts_exist(envelope(artifacts=["report.md"]), self.run_for())
        self.assertFalse(report.passed)

    def test_a_real_artifact_passes_and_records_its_size(self):
        self.write("report.md", "findings\n")
        report = gates.artifacts_exist(envelope(artifacts=["report.md"]), self.run_for())
        self.assertTrue(report.passed, report.violations)
        self.assertIn("exists", report.checks[0].note)

    def test_files_non_empty_skips_what_it_cannot_resolve(self):
        """Out-of-root and missing paths belong to artifacts_exist, not here."""
        run = self.run_for()
        report = gates.files_non_empty(envelope(artifacts=["/etc/hosts", "gone.md"]), run)
        self.assertTrue(report.passed, report.violations)

    def test_files_non_empty_still_catches_an_empty_artifact(self):
        self.write("report.md", "")
        report = gates.files_non_empty(envelope(artifacts=["report.md"]), self.run_for())
        self.assertFalse(report.passed)

    def test_json_parses_resolves_through_the_same_roots(self):
        self.write("out.json", "{not json")
        report = gates.json_parses(envelope(artifacts=["out.json"]), self.run_for())
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
