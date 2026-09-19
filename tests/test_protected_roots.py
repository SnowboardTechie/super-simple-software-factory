"""Separating the roots must not un-protect the ones an agent may never touch.

`protected_files` guards the factory's own source — but it is matched against
paths in the tree `snapshot()` looked at, and `snapshot()` looked at exactly one
tree. So the moment `control_root` became a DIFFERENT repository from
`target_worktree`, rewriting `adws/adw_modules/gates.py` produced no detected
path at all: the guard was still there, aimed at the wrong directory. The same
hole opened between a linked review worktree and the trunk it was cut from.

Every root except `state_root` is watched. `state_root` is the run's own
runtime and is the one place every agent must be able to write.
"""

from __future__ import annotations

import unittest

from support import GitFixture, git

from adw_modules import permissions
from adw_modules.data_types import AgentConfig, PromptEngineering, SSSFConfig

READ_ONLY = AgentConfig(name="reviewer", writes=[],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
UNRESTRICTED = AgentConfig(name="builder", writes=None,
                           prompt_engineering=PromptEngineering(system="s.md", user="u.md"))


class Phase:
    class params:
        name = "review"


class ControlRootTest(GitFixture):
    """control_root is the factory: its source is never an agent's to change."""

    def setUp(self):
        super().setUp()
        self.control = self.make_repo("factory")
        (self.control / "adws" / "adw_modules").mkdir(parents=True)
        (self.control / "adws" / "adw_modules" / "gates.py").write_text("def strict(): ...\n")
        git("add", "-A", cwd=self.control)
        git("commit", "-qm", "factory source", cwd=self.control)
        self.runner = self.run_for(control_root=self.control, target_repo=self.root)

    def control_file(self, rel: str):
        return self.control / rel

    def test_a_control_root_change_is_detected(self):
        before = permissions.snapshot(self.runner)
        self.control_file("adws/adw_modules/gates.py").write_text("def everything_passes(): ...\n")

        detected = permissions.changed_paths(before, permissions.snapshot(self.runner))

        self.assertTrue(detected, "a distinct control root must still be watched")
        self.assertTrue(any("adw_modules/gates.py" in p for p in detected), detected)

    def test_a_control_root_change_breaches_even_an_unrestricted_agent(self):
        """`writes: None` means unrestricted in the TARGET, never in the factory."""
        before = permissions.snapshot(self.runner)
        self.control_file("adws/adw_modules/gates.py").write_text("def everything_passes(): ...\n")

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(self.runner, Phase(), UNRESTRICTED, before)
        self.assertIn("gates.py", str(caught.exception))

    def test_an_introduced_control_root_change_is_rolled_back_byte_for_byte(self):
        original = self.control_file("adws/adw_modules/gates.py").read_text()
        before = permissions.snapshot(self.runner)
        self.control_file("adws/adw_modules/gates.py").write_text("tampered\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(self.runner, Phase(), READ_ONLY, before)
        self.assertEqual(self.control_file("adws/adw_modules/gates.py").read_text(), original)

    def test_an_introduced_untracked_control_file_is_deleted(self):
        before = permissions.snapshot(self.runner)
        (self.control / "adws" / "adw_evil.py").write_text("backdoor\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(self.runner, Phase(), READ_ONLY, before)
        self.assertFalse((self.control / "adws" / "adw_evil.py").exists())

    def test_pre_existing_dirty_control_bytes_are_preserved_not_reconstructed(self):
        """Same rule as the target: the cleanup never destroys the engineer's edit."""
        self.control_file("adws/adw_modules/gates.py").write_text("engineer mid-edit\n")
        before = permissions.snapshot(self.runner)
        self.control_file("adws/adw_modules/gates.py").write_text("agent overwrote it\n")

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(self.runner, Phase(), READ_ONLY, before)
        # Named as already-modified and left exactly as found: restoring the
        # committed bytes here would destroy the engineer's edit as well.
        self.assertIn("already modified", str(caught.exception))
        self.assertEqual(self.control_file("adws/adw_modules/gates.py").read_text(),
                         "agent overwrote it\n")

    def test_the_runtime_state_root_is_still_freely_writable(self):
        """state_root lives INSIDE control_root by default; it must not breach."""
        before = permissions.snapshot(self.runner)
        handoff = self.runner.targets.state_root / "sessions" / "a1" / "context_handoff"
        handoff.mkdir(parents=True)
        (handoff / "findings.md").write_text("the reviewer's own report\n")

        self.assertEqual(permissions.enforce(self.runner, Phase(), READ_ONLY, before), [])

    def test_bytecode_in_the_control_root_IS_a_breach_and_is_deleted(self):
        """A .pyc is executable Python. Dropping one in the factory is the exact
        thing protected_files exists to stop, so the build-noise exemption must
        not follow the guard into a protected root."""
        before = permissions.snapshot(self.runner)
        cache = self.control / "adws" / "adw_modules" / "__pycache__"
        cache.mkdir()
        (cache / "gates.cpython-313.pyc").write_text("\x00bytecode\n")

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(self.runner, Phase(), READ_ONLY, before)
        self.assertIn("gates.cpython-313.pyc", str(caught.exception))
        self.assertFalse((cache / "gates.cpython-313.pyc").exists())

    def test_bytecode_never_reaches_a_commit_phase(self):
        self.runner.tree_baseline = permissions.snapshot(self.runner)
        cache = self.control / "adws" / "adw_modules" / "__pycache__"
        cache.mkdir()
        (cache / "gates.cpython-313.pyc").write_text("\x00bytecode\n")

        self.assertEqual(permissions.repo_changes(self.runner), [])


class TargetTrunkTest(GitFixture):
    """A review worktree's trunk checkout is not the worktree, and not fair game."""

    def setUp(self):
        super().setUp()
        self.worktree = self.add_worktree("review-worktree")
        self.runner = self.run_for(target_repo=self.root, target_worktree=self.worktree)

    def test_a_trunk_change_is_detected_and_refused(self):
        before = permissions.snapshot(self.runner)
        (self.root / "README.md").write_text("the agent edited the trunk\n")

        detected = permissions.changed_paths(before, permissions.snapshot(self.runner))
        self.assertTrue(any("README.md" in p for p in detected), detected)
        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(self.runner, Phase(), UNRESTRICTED, before)

    def test_the_trunk_is_restored_when_the_change_was_introduced(self):
        original = (self.root / "README.md").read_text()
        before = permissions.snapshot(self.runner)
        (self.root / "README.md").write_text("the agent edited the trunk\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(self.runner, Phase(), UNRESTRICTED, before)
        self.assertEqual((self.root / "README.md").read_text(), original)

    def test_work_inside_the_review_worktree_is_judged_normally(self):
        before = permissions.snapshot(self.runner)
        (self.worktree / "src").mkdir()
        (self.worktree / "src" / "feature.py").write_text("allowed\n")

        self.assertEqual(permissions.enforce(self.runner, Phase(), UNRESTRICTED, before),
                         ["src/feature.py"])

    def test_a_protected_root_change_never_enters_the_accepted_change_set(self):
        """Nothing outside the target worktree may reach a commit phase."""
        self.runner.tree_baseline = permissions.snapshot(self.runner)
        (self.root / "README.md").write_text("trunk edit\n")
        (self.worktree / "src").mkdir()
        (self.worktree / "src" / "feature.py").write_text("real work\n")

        self.assertEqual(permissions.repo_changes(self.runner), ["src/feature.py"])


class SharedCheckoutTest(GitFixture):
    """control_root and target_repo can be the SAME checkout. Watch it once.

    With a review worktree cut from the repo the factory lives in, both labels
    resolved to that one checkout — so every change in it was detected twice,
    reported twice, and rolled back twice. The second rollback acts on a path
    the first one already dealt with.
    """

    def setUp(self):
        super().setUp()
        self.worktree = self.add_worktree("review-worktree")
        # control == trunk == self.root; the agent works in the linked worktree.
        self.runner = self.run_for(control_root=self.root, target_repo=self.root,
                                   target_worktree=self.worktree)

    def test_one_checkout_is_watched_under_exactly_one_label(self):
        self.assertEqual(list(self.runner.targets.protected_roots), ["control"])
        self.assertEqual(self.runner.targets.protected_roots["control"], self.root)

    def test_a_change_is_detected_once_not_twice(self):
        before = permissions.snapshot(self.runner)
        (self.root / "scratch.txt").write_text("introduced by the agent\n")

        detected = permissions.changed_paths(before, permissions.snapshot(self.runner))
        self.assertEqual([k for k in detected if k.endswith("scratch.txt")],
                         ["control::scratch.txt"])

    def test_an_introduced_untracked_file_is_rolled_back_once(self):
        before = permissions.snapshot(self.runner)
        (self.root / "scratch.txt").write_text("introduced by the agent\n")

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(self.runner, Phase(), UNRESTRICTED, before)

        message = str(caught.exception)
        self.assertEqual(message.count("scratch.txt"), 1, message)
        self.assertIn("1 path(s)", message)
        self.assertIn("deleted", message)
        # Deleted exactly once — a second attempt would have reported a failure
        # to delete a file that was already gone.
        self.assertNotIn("could not delete", message)
        self.assertFalse((self.root / "scratch.txt").exists())

    def test_the_worktree_is_still_judged_on_its_own(self):
        before = permissions.snapshot(self.runner)
        (self.worktree / "src").mkdir()
        (self.worktree / "src" / "feature.py").write_text("real work\n")

        self.assertEqual(permissions.enforce(self.runner, Phase(), UNRESTRICTED, before),
                         ["src/feature.py"])


class SingleRootTest(GitFixture):
    """The default layout has nothing extra to watch, and behaves exactly as before."""

    def test_no_protected_roots_when_every_root_collapses(self):
        run = self.run_for()
        self.assertEqual(run.targets.protected_roots, {})

    def test_detection_keys_stay_plain_repo_relative_paths(self):
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("src/app.py", "changed\n")
        self.assertEqual(permissions.changed_paths(before, permissions.snapshot(run)),
                         ["src/app.py"])


if __name__ == "__main__":
    unittest.main()
