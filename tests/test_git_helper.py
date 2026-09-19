"""Commit phases stage an explicit path set, and nothing else.

`commit_all()` ran `git add -A`, so a commit phase swept up whatever the
engineer happened to have uncommitted — their half-finished edit, their scratch
file, their unrelated branch work — and attributed it to the agent's message.
These tests pin the replacement: a commit stages exactly the paths it was
handed, refuses paths that are not actually changed, and leaves everything else
in the working tree where it was.
"""

from __future__ import annotations

import unittest

from support import GitFixture, git

from adw_modules import git_helper


class CommitPathsTest(GitFixture):

    def test_pre_existing_unrelated_path_is_not_staged(self):
        """The engineer's uncommitted work survives an agent's commit phase."""
        self.write("README.md", "engineer was mid-sentence\n")   # unrelated, dirty
        self.write("src/feature.py", "def feature(): ...\n")     # the agent's work

        sha = git_helper.commit_paths("agent work", ["src/feature.py"], cwd=self.root)

        landed = git("show", "--pretty=format:", "--name-only", "--no-renames", sha,
                     cwd=self.root).split()
        self.assertEqual(landed, ["src/feature.py"])
        self.assertEqual(self.read("README.md"), "engineer was mid-sentence\n")
        self.assertIn("README.md", git("status", "--porcelain", cwd=self.root))

    def test_commit_all_is_gone(self):
        """No compatibility shim may keep `git add -A` reachable."""
        self.assertFalse(hasattr(git_helper, "commit_all"))

    def test_rejects_a_path_that_is_not_actually_changed(self):
        """An invented path in the accepted set fails the phase, commits nothing."""
        self.write("src/feature.py", "real\n")
        head = git("rev-parse", "HEAD", cwd=self.root)

        with self.assertRaises(RuntimeError) as caught:
            git_helper.commit_paths("agent work",
                                    ["src/feature.py", "src/imagined.py"], cwd=self.root)

        self.assertIn("src/imagined.py", str(caught.exception))
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root), head)

    def test_rejects_an_empty_path_set(self):
        """A phase that changed nothing fails rather than committing nothing."""
        with self.assertRaises(RuntimeError):
            git_helper.commit_paths("agent work", [], cwd=self.root)

    def test_rejects_a_path_outside_the_repository(self):
        self.write("src/feature.py", "real\n")
        for escape in ("../outside.txt", "/etc/hosts"):
            with self.subTest(escape=escape), self.assertRaises(RuntimeError):
                git_helper.commit_paths("agent work", [escape], cwd=self.root)

    def test_commits_a_deletion_and_an_untracked_file_together(self):
        (self.root / "README.md").unlink()
        self.write("src/new.py", "new\n")

        sha = git_helper.commit_paths("agent work", ["README.md", "src/new.py"],
                                      cwd=self.root)

        landed = sorted(git("show", "--pretty=format:", "--name-only", "--no-renames",
                            sha, cwd=self.root).split())
        self.assertEqual(landed, ["README.md", "src/new.py"])

    def test_directory_pathspec_cannot_widen_the_commit(self):
        """`src/` as a pathspec would sweep in a sibling the agent never touched."""
        self.write("src/agent.py", "agent\n")
        self.write("src/engineer_scratch.py", "not the agent's\n")

        with self.assertRaises(RuntimeError):
            git_helper.commit_paths("agent work", ["src"], cwd=self.root)


class CwdIsExplicitTest(GitFixture):
    """Every git call names the repository it means, instead of inheriting one."""

    def test_git_helpers_answer_about_the_named_repository(self):
        self.write("src/feature.py", "real\n")
        self.assertTrue(git_helper.is_repo(cwd=self.root))
        self.assertTrue(git_helper.is_dirty(cwd=self.root))
        self.assertEqual(git_helper.repo_root(cwd=self.root), self.root)
        self.assertEqual(git_helper.current_branch(cwd=self.root), "main")
        self.assertIn("src/feature.py", git_helper.untracked_files(cwd=self.root))

    def test_dirty_paths_reports_tracked_untracked_and_deleted(self):
        self.write("tracked.txt", "one\n")
        self.commit()
        self.write("tracked.txt", "two\n")
        self.write("fresh.txt", "new\n")
        (self.root / "README.md").unlink()

        self.assertEqual(git_helper.dirty_paths(cwd=self.root),
                         ["README.md", "fresh.txt", "tracked.txt"])

    def test_dirty_paths_can_include_ignored_files(self):
        self.write("secrets.env", "API_KEY=x\n")
        self.assertNotIn("secrets.env", git_helper.dirty_paths(cwd=self.root))
        self.assertIn("secrets.env",
                      git_helper.dirty_paths(cwd=self.root, include_ignored=True))


if __name__ == "__main__":
    unittest.main()
