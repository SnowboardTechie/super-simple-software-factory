"""What an agent changed is measured by content, not by line counts.

`snapshot()` fingerprinted tracked files as `"<added>,<removed>"` from
`git diff --numstat`. Two different rewrites of the same already-dirty file
produce the same counts, so an agent could replace an engineer's uncommitted
work byte-for-byte and the comparison would see nothing. These tests pin the
fingerprint to git's own content identity, extend the watch to ignored files
(where `.env` lives), and keep the existing rule that pre-existing dirty bytes
are never reconstructed by the cleanup.
"""

from __future__ import annotations

import unittest

from support import GitFixture, git

from adw_modules import permissions
from adw_modules.data_types import AgentConfig, PromptEngineering, SSSFConfig

READ_ONLY = AgentConfig(name="reviewer", writes=[],
                        prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
BUILDER = AgentConfig(name="builder", writes=["src/"],
                      prompt_engineering=PromptEngineering(system="s.md", user="u.md"))


class Phase:
    """permissions.enforce only ever reads the phase for its name in messages."""
    class params:
        name = "build"


class ContentFingerprintTest(GitFixture):

    def test_same_numstat_different_bytes_is_detected(self):
        """The case numstat cannot see: a rewrite with the same shape."""
        self.write("src/app.py", "line one\nline two\nline three\n")
        self.commit()
        self.write("src/app.py", "AAA\nline two\nline three\n")     # engineer, +1/-1
        run = self.run_for()

        before = permissions.snapshot(run)
        self.write("src/app.py", "BBB\nline two\nline three\n")     # agent, also +1/-1
        after = permissions.snapshot(run)

        self.assertEqual(permissions.changed_paths(before, after), ["src/app.py"])

    def test_identical_rewrite_is_not_a_change(self):
        """Content identity, so writing the same bytes back is correctly silent."""
        self.write("src/app.py", "same\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("src/app.py", "same\n")
        self.assertEqual(permissions.changed_paths(before, permissions.snapshot(run)), [])

    def test_ignored_files_are_fingerprinted(self):
        """`.env`-shaped files are gitignored, and are exactly what must not move."""
        self.write("secrets.env", "API_KEY=original\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("secrets.env", "API_KEY=stolen\n")

        self.assertIn("secrets.env",
                      permissions.changed_paths(before, permissions.snapshot(run)))

    def test_a_mode_only_change_is_detected(self):
        """chmod +x rewrites no byte, so content identity alone sees nothing."""
        self.write("scripts/run.sh", "#!/bin/sh\necho hi\n")
        self.commit()
        run = self.run_for()
        before = permissions.snapshot(run)
        (self.root / "scripts" / "run.sh").chmod(0o755)

        self.assertEqual(permissions.changed_paths(before, permissions.snapshot(run)),
                         ["scripts/run.sh"])

    def test_a_mode_only_change_to_an_ALREADY_DIRTY_file_is_detected(self):
        """The dangerous case: the path is already in the diff, so only the
        fingerprint's mode component can tell that anything moved."""
        self.write("scripts/run.sh", "#!/bin/sh\necho hi\n")
        self.commit()
        self.write("scripts/run.sh", "#!/bin/sh\necho engineer\n")   # dirty already
        run = self.run_for()
        before = permissions.snapshot(run)
        (self.root / "scripts" / "run.sh").chmod(0o755)

        self.assertEqual(permissions.changed_paths(before, permissions.snapshot(run)),
                         ["scripts/run.sh"])

    def test_dropping_the_executable_bit_is_detected_too(self):
        self.write("scripts/run.sh", "#!/bin/sh\n")
        (self.root / "scripts" / "run.sh").chmod(0o755)
        self.commit()
        run = self.run_for()
        before = permissions.snapshot(run)
        (self.root / "scripts" / "run.sh").chmod(0o644)

        self.assertEqual(permissions.changed_paths(before, permissions.snapshot(run)),
                         ["scripts/run.sh"])

    def test_a_no_op_chmod_is_not_a_change(self):
        """Negative: re-applying the same mode must stay silent."""
        self.write("scripts/run.sh", "#!/bin/sh\n")
        (self.root / "scripts" / "run.sh").chmod(0o755)
        run = self.run_for()
        before = permissions.snapshot(run)
        (self.root / "scripts" / "run.sh").chmod(0o755)

        self.assertEqual(permissions.changed_paths(before, permissions.snapshot(run)), [])

    def test_replacing_a_file_with_a_symlink_is_detected(self):
        """Same bytes through the link; the TYPE is what changed."""
        self.write("target.txt", "payload\n")
        self.write("thing.txt", "payload\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        (self.root / "thing.txt").unlink()
        (self.root / "thing.txt").symlink_to(self.root / "target.txt")

        self.assertIn("thing.txt",
                      permissions.changed_paths(before, permissions.snapshot(run)))

    def test_a_mode_change_still_breaches_a_read_only_agent(self):
        self.write("scripts/run.sh", "#!/bin/sh\n")
        self.commit()
        run = self.run_for()
        before = permissions.snapshot(run)
        (self.root / "scripts" / "run.sh").chmod(0o755)

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), READ_ONLY, before)

    def test_build_noise_is_exempt(self):
        """Every python import writes bytecode; that is not a mutation."""
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("src/__pycache__/app.cpython-313.pyc", "\x00bytecode\n")
        touched = permissions.changed_paths(before, permissions.snapshot(run))

        self.assertTrue(all(permissions.permitted(p, READ_ONLY, run.cfg) for p in touched),
                        f"bytecode must not breach a read-only agent: {touched}")

    def test_session_runtime_is_not_a_repo_change(self):
        """An agent's own report is not a change to the repository under review."""
        cfg = SSSFConfig()
        run = self.run_for(cfg)
        run.tree_baseline = permissions.snapshot(run)
        self.write(f"{cfg.defaults.data_dir}/sessions/x/context_handoff/notes.md", "hi\n")

        self.assertEqual(permissions.repo_changes(run), [])

    def test_preexisting_index_flags_cannot_hide_a_rewrite(self):
        self.write("assume-hidden.txt", "operator assume bytes\n")
        self.write("skip-hidden.txt", "operator skip bytes\n")
        self.commit()
        git("update-index", "--assume-unchanged", "assume-hidden.txt", cwd=self.root)
        git("update-index", "--skip-worktree", "skip-hidden.txt", cwd=self.root)
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("assume-hidden.txt", "hidden assume tamper\n")
        self.write("skip-hidden.txt", "hidden skip tamper\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), READ_ONLY, before)

        self.assertEqual(self.read("assume-hidden.txt"), "operator assume bytes\n")
        self.assertEqual(self.read("skip-hidden.txt"), "operator skip bytes\n")
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_a_hidden_preexisting_dirty_file_is_never_reset_to_committed_bytes(self):
        self.write("hidden-dirty.txt", "committed bytes\n")
        self.commit()
        git("update-index", "--assume-unchanged", "hidden-dirty.txt", cwd=self.root)
        self.write("hidden-dirty.txt", "operator bytes\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("hidden-dirty.txt", "agent overwrote operator bytes\n")

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(run, Phase(), READ_ONLY, before)

        self.assertIn("already modified", str(caught.exception))
        self.assertEqual(self.read("hidden-dirty.txt"),
                         "agent overwrote operator bytes\n")


class EnforcementTest(GitFixture):

    def test_read_only_agent_rewriting_a_dirty_file_is_a_breach(self):
        self.write("src/app.py", "committed\n")
        self.commit()
        self.write("src/app.py", "engineer's uncommitted work\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("src/app.py", "the reviewer quietly fixed it\n")

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(run, Phase(), READ_ONLY, before)
        self.assertIn("src/app.py", str(caught.exception))

    def test_a_pre_existing_dirty_path_is_never_reconstructed(self):
        """The cleanup must not commit the harm it exists to prevent."""
        self.write("src/app.py", "committed\n")
        self.commit()
        self.write("src/app.py", "engineer's uncommitted work\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("src/app.py", "agent overwrote it\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), READ_ONLY, before)
        # Left exactly as the agent left it, and said so — not silently reverted
        # to the committed bytes, which would destroy the engineer's edit too.
        self.assertEqual(self.read("src/app.py"), "agent overwrote it\n")

    def test_an_introduced_untracked_file_is_rolled_back(self):
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("docs/sneaky.md", "not mine to write\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), READ_ONLY, before)
        self.assertFalse((self.root / "docs" / "sneaky.md").exists())

    def test_an_ignored_file_mutation_is_rolled_back_when_introduced(self):
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("secrets.env", "API_KEY=written-by-agent\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), READ_ONLY, before)
        self.assertFalse((self.root / "secrets.env").exists())

    def test_an_allowed_path_is_returned_not_raised(self):
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("src/feature.py", "allowed\n")

        self.assertEqual(permissions.enforce(run, Phase(), BUILDER, before),
                         ["src/feature.py"])

    def test_factory_source_stays_protected(self):
        run = self.run_for()
        before = permissions.snapshot(run)
        self.write("adws/adw_modules/gates.py", "def everything_passes(): pass\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), BUILDER, before)
        self.assertFalse((self.root / "adws" / "adw_modules" / "gates.py").exists())

    def test_reverting_the_engineers_work_is_still_a_breach(self):
        """`git checkout -- path` leaves a clean tree; a clean tree is a change."""
        self.write("src/app.py", "committed\n")
        self.commit()
        self.write("src/app.py", "engineer's uncommitted work\n")
        run = self.run_for()
        before = permissions.snapshot(run)
        git("checkout", "--", "src/app.py", cwd=self.root)

        with self.assertRaises(permissions.PermissionBreach) as caught:
            permissions.enforce(run, Phase(), READ_ONLY, before)
        self.assertIn("REVERTED-BY-AGENT", str(caught.exception))

    def test_a_separated_runtime_does_not_exempt_the_same_relative_target_path(self):
        control = self.make_repo("factory")
        state = control / "adws" / "adw_data"
        self.write("adws/adw_data/payload.txt", "target bytes\n")
        self.commit()
        run = self.run_for(control_root=control, target_repo=self.root,
                           state_root=state)
        before = permissions.snapshot(run)
        self.write("adws/adw_data/payload.txt", "tampered target bytes\n")

        with self.assertRaises(permissions.PermissionBreach):
            permissions.enforce(run, Phase(), READ_ONLY, before)

        self.assertEqual(self.read("adws/adw_data/payload.txt"), "target bytes\n")


if __name__ == "__main__":
    unittest.main()
