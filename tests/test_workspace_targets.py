"""A run distinguishes four roots, and artifacts may only land inside them.

Upstream had one root: `git rev-parse --show-toplevel` from wherever the
process started. That conflates the workspace the factory is installed in, the
runtime state it writes, and the repository it is working ON — so a workflow
that reviews a checkout somewhere else cannot be expressed at all, and a
declared artifact is any path the process can reach.
"""

from __future__ import annotations

import ast
import os
import subprocess
import unittest

from support import ADWS, REPO_ROOT, GitFixture, git

from adw_modules.data_types import RunTargets, SSSFConfig


class SeparateRootsTest(GitFixture):

    def test_a_workflow_can_name_a_target_distinct_from_the_workspace(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        targets = RunTargets.resolve(SSSFConfig(), control_root=workspace,
                                     target_repo=self.root)

        self.assertEqual(targets.control_root, workspace)
        self.assertEqual(targets.target_repo, self.root)
        self.assertEqual(targets.target_worktree, self.root)
        self.assertTrue(str(targets.state_root).startswith(str(workspace)))

    def test_a_linked_worktree_is_the_checkout_agents_touch(self):
        """A review worktree shares the repository but is its own checkout."""
        worktree = self.root.parent / "review-worktree"
        git("worktree", "add", "-q", "-b", "review", str(worktree), cwd=self.root)
        self.addCleanup(git, "worktree", "remove", "--force", str(worktree), cwd=self.root)

        targets = RunTargets.resolve(SSSFConfig(), control_root=self.root,
                                     target_repo=self.root,
                                     target_worktree=worktree)

        self.assertEqual(targets.target_repo, self.root)
        self.assertEqual(targets.target_worktree, worktree.resolve())

    def test_defaults_collapse_to_the_current_repository(self):
        """Unchanged behaviour for every existing ADW: one root, resolved from cwd."""
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

        targets = RunTargets.resolve(SSSFConfig())

        self.assertEqual(targets.control_root, self.root)
        self.assertEqual(targets.target_repo, self.root)
        self.assertEqual(targets.target_worktree, self.root)

    def test_a_missing_target_is_refused(self):
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), target_repo=self.root / "nope")


class TargetWorktreeIdentityTest(GitFixture):
    """A named worktree must BELONG to the named repository.

    Accepting any directory that happened to be a git repo meant a workflow
    could be pointed at an unrelated checkout — the identity the whole run is
    about, decided by a typo. A linked worktree shares its repository's git
    common directory; nothing else does.
    """

    def test_a_linked_worktree_of_the_named_repo_is_accepted(self):
        worktree = self.add_worktree("linked")
        targets = RunTargets.resolve(SSSFConfig(), target_repo=self.root,
                                     target_worktree=worktree)
        self.assertEqual(targets.target_worktree, worktree)
        self.assertEqual(targets.target_repo, self.root)

    def test_an_unrelated_repository_is_refused(self):
        other = self.make_repo("unrelated")
        with self.assertRaises(ValueError) as caught:
            RunTargets.resolve(SSSFConfig(), target_repo=self.root, target_worktree=other)
        self.assertIn("unrelated", str(caught.exception).lower())

    def test_a_non_git_directory_is_refused(self):
        plain = self.root.parent / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), target_repo=self.root, target_worktree=plain)

    def test_a_subdirectory_of_the_repository_is_refused(self):
        """`src/` is inside the repo, but it is not a worktree ROOT."""
        (self.root / "src").mkdir()
        with self.assertRaises(ValueError) as caught:
            RunTargets.resolve(SSSFConfig(), target_repo=self.root,
                               target_worktree=self.root / "src")
        self.assertIn("root", str(caught.exception).lower())

    def test_a_non_git_target_repo_with_no_worktree_named_is_still_allowed(self):
        """ADWs run fine outside git; only a commit phase needs a repository."""
        plain = self.root.parent / "plain-workspace"
        plain.mkdir()
        targets = RunTargets.resolve(SSSFConfig(), control_root=plain, target_repo=plain)
        self.assertEqual(targets.target_worktree, plain.resolve())


class TargetRepoIdentityTest(GitFixture):
    """A target inside a checkout must BE that checkout's root.

    The worktree check only ran when a separate worktree was named, so
    `target_repo=<repo>/src` sailed through untouched — and every git question
    the run then asked answered about the whole repository while the run
    believed its target was one directory. A non-git target directory stays
    legal: only a commit phase needs a repository.
    """

    def test_a_subdirectory_of_a_checkout_is_refused_as_the_target_repo(self):
        (self.root / "src").mkdir()
        with self.assertRaises(ValueError) as caught:
            RunTargets.resolve(SSSFConfig(), target_repo=self.root / "src")
        self.assertIn("root", str(caught.exception).lower())

    def test_a_nested_directory_is_refused_too(self):
        (self.root / "src" / "deep").mkdir(parents=True)
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), target_repo=self.root / "src" / "deep")

    def test_the_checkout_root_itself_is_accepted(self):
        targets = RunTargets.resolve(SSSFConfig(), target_repo=self.root)
        self.assertEqual(targets.target_repo, self.root)

    def test_a_linked_worktree_root_is_accepted_as_the_target_repo(self):
        """It is the canonical root of its own checkout, so it stands alone."""
        worktree = self.add_worktree("standalone")
        targets = RunTargets.resolve(SSSFConfig(), target_repo=worktree)
        self.assertEqual(targets.target_repo, worktree)

    def test_a_non_git_directory_is_still_a_legal_target(self):
        plain = self.workspace / "plain"
        plain.mkdir()
        targets = RunTargets.resolve(SSSFConfig(), control_root=plain, target_repo=plain)
        self.assertEqual(targets.target_repo, plain.resolve())

    def test_a_default_target_taken_from_a_subdirectory_normalises_to_the_root(self):
        """v1 resolved the git toplevel from cwd; launching from `src/` still works."""
        (self.root / "src").mkdir()
        cwd = os.getcwd()
        os.chdir(self.root / "src")
        self.addCleanup(os.chdir, cwd)

        targets = RunTargets.resolve(SSSFConfig())

        self.assertEqual(targets.target_repo, self.root)
        self.assertEqual(targets.control_root, self.root)
        self.assertEqual(targets.target_worktree, self.root)
        self.assertEqual(targets.protected_roots, {})


class StateRootTopologyTest(GitFixture):
    """The runtime may live inside a watched root, or beside it — never around it.

    `snapshot()` drops every path under `state_root`, because the runtime is the
    one place agents must be able to write. A state root that CONTAINS a watched
    root therefore silenced that whole root: every change in it read as runtime,
    and nothing was ever detected.
    """

    def test_a_state_root_equal_to_the_control_root_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            RunTargets.resolve(SSSFConfig(), control_root=self.root,
                               target_repo=self.root, state_root=self.root)
        self.assertIn("state root", str(caught.exception).lower())

    def test_a_state_root_containing_a_watched_root_is_refused(self):
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), control_root=self.root,
                               target_repo=self.root, state_root=self.workspace)

    def test_a_state_root_equal_to_the_target_worktree_is_refused(self):
        worktree = self.add_worktree("review")
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), control_root=self.root,
                               target_repo=self.root, target_worktree=worktree,
                               state_root=worktree)

    def test_a_state_root_containing_the_target_worktree_is_refused(self):
        worktree = self.add_worktree("review")
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), control_root=self.root,
                               target_repo=self.root, target_worktree=worktree,
                               state_root=self.workspace)

    # ── the two supported layouts ───────────────────────────────────────────
    def test_the_default_state_root_inside_the_control_root_is_supported(self):
        targets = RunTargets.resolve(SSSFConfig(), control_root=self.root,
                                     target_repo=self.root)
        self.assertEqual(targets.state_root, self.root / "adws" / "adw_data")

    def test_a_disjoint_sibling_state_root_is_supported(self):
        state = self.workspace / "runtime"
        state.mkdir()
        targets = RunTargets.resolve(SSSFConfig(), control_root=self.root,
                                     target_repo=self.root, state_root=state)
        self.assertEqual(targets.state_root, state.resolve())

    def test_a_state_root_that_does_not_exist_yet_is_still_checked(self):
        """The first run creates it, so the topology must be judged on the path."""
        with self.assertRaises(ValueError):
            RunTargets.resolve(SSSFConfig(), control_root=self.root,
                               target_repo=self.root,
                               state_root=self.workspace / "nope" / "..")


class TraceDbLocationTest(GitFixture):
    """The trace must live where the runtime lives, or the UI looks in the wrong place.

    The visualizer derives `sessions/` from the db's own directory. A relative
    db resolved against `control_root` therefore sent the UI hunting beside the
    factory while the sessions were written under a separated `state_root`.
    """

    def test_the_default_single_root_path_is_unchanged(self):
        cfg = SSSFConfig()
        targets = RunTargets.resolve(cfg, control_root=self.root, target_repo=self.root)
        self.assertEqual(targets.trace_db(cfg),
                         self.root / "adws" / "adw_data" / "sssf.db")
        self.assertEqual(targets.trace_db(cfg).parent, targets.state_root)

    def test_a_separated_state_root_contains_both_the_db_and_the_sessions(self):
        cfg = SSSFConfig()
        state = self.root.parent / "runtime"
        state.mkdir()
        targets = RunTargets.resolve(cfg, control_root=self.root, target_repo=self.root,
                                     state_root=state)

        self.assertEqual(targets.trace_db(cfg), state.resolve() / "sssf.db")
        self.assertEqual(targets.trace_db(cfg).parent, targets.state_root)
        self.assertEqual(targets.events_jsonl("a1").parent.parent,
                         targets.state_root / "sessions")

    def test_a_relative_db_outside_the_data_dir_still_lands_in_the_state_root(self):
        cfg = SSSFConfig()
        cfg.observability.db = "traces/sssf.db"
        state = self.root.parent / "runtime2"
        state.mkdir()
        targets = RunTargets.resolve(cfg, control_root=self.root, target_repo=self.root,
                                     state_root=state)
        self.assertEqual(targets.trace_db(cfg), state.resolve() / "traces" / "sssf.db")

    def test_an_absolute_db_inside_the_state_root_is_accepted(self):
        cfg = SSSFConfig()
        state = self.root / "adws" / "adw_data"
        cfg.observability.db = str(state / "sssf.db")
        targets = RunTargets.resolve(cfg, control_root=self.root, target_repo=self.root)
        self.assertEqual(targets.trace_db(cfg), state / "sssf.db")

    def test_an_absolute_db_outside_the_state_root_is_refused(self):
        cfg = SSSFConfig()
        cfg.observability.db = str(self.root.parent / "escaped.db")
        state = self.root.parent / "runtime3"
        state.mkdir()
        targets = RunTargets.resolve(cfg, control_root=self.root, target_repo=self.root,
                                     state_root=state)
        with self.assertRaises(ValueError) as caught:
            targets.trace_db(cfg)
        self.assertIn("state root", str(caught.exception).lower())

    def test_a_relative_db_cannot_traverse_out_of_the_state_root(self):
        cfg = SSSFConfig()
        cfg.observability.db = "../../escaped.db"
        targets = RunTargets.resolve(cfg, control_root=self.root, target_repo=self.root)
        with self.assertRaises(ValueError):
            targets.trace_db(cfg)


class ArtifactRootTest(GitFixture):

    def setUp(self):
        super().setUp()
        self.targets = RunTargets.resolve(SSSFConfig(), control_root=self.root,
                                          target_repo=self.root)

    def test_a_relative_artifact_resolves_under_the_target_worktree(self):
        self.write("notes.md", "hi\n")
        self.assertEqual(self.targets.resolve_artifact("notes.md"), self.root / "notes.md")

    def test_an_artifact_in_the_state_root_is_allowed(self):
        handoff = self.targets.state_root / "sessions" / "a1" / "context_handoff" / "plan.md"
        handoff.parent.mkdir(parents=True)
        handoff.write_text("plan\n")
        self.assertEqual(self.targets.resolve_artifact(str(handoff)), handoff)

    def test_traversal_out_of_the_roots_is_refused(self):
        for escape in ("../outside.md", "../../etc/hosts", "/etc/hosts"):
            with self.subTest(escape=escape), self.assertRaises(ValueError):
                self.targets.resolve_artifact(escape)

    def test_a_symlink_that_escapes_the_roots_is_refused(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("elsewhere\n")
        self.addCleanup(outside.unlink)
        (self.root / "link.md").symlink_to(outside)

        with self.assertRaises(ValueError):
            self.targets.resolve_artifact("link.md")

    def test_an_empty_declaration_is_refused(self):
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)), self.assertRaises(ValueError):
                self.targets.resolve_artifact(blank)


class InstallerContractTest(unittest.TestCase):
    """The stamped layout is the engine's public interface; keep it stable."""

    def test_every_module_the_installer_stamps_still_exists(self):
        modules = ADWS / "adw_modules"
        for name in ("git_helper.py", "permissions.py", "gates.py", "runner.py",
                     "agents.py", "data_types.py", "tracer.py", "redact.py"):
            with self.subTest(module=name):
                self.assertTrue((modules / name).is_file(), name)

    def test_every_required_module_is_tracked_not_only_present_locally(self):
        relative = ".claude/skills/sssf/templates/adws/adw_modules/redact.py"
        result = subprocess.run(["git", "ls-files", "--error-unmatch", "--", relative],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"installer dependency is absent from Git: {relative}")

    def test_every_plan_build_test_builder_checks_its_changed_file_claim(self):
        source = (ADWS / "adw_plan_build_test.py").read_text()
        tree = ast.parse(source)
        builder_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "AgentCall":
                continue
            output = next((kw.value for kw in node.keywords if kw.arg == "output_type"), None)
            if isinstance(output, ast.Name) and output.id == "BuildOutput":
                builder_calls.append(node)
        self.assertEqual(len(builder_calls), 2)
        for call in builder_calls:
            gates_kw = next((kw.value for kw in call.keywords if kw.arg == "gates"), None)
            names = {elt.attr for elt in getattr(gates_kw, "elts", [])
                     if isinstance(elt, ast.Attribute)}
            self.assertIn("changed_files_match", names)

    def test_the_fork_records_its_upstream_base_and_divergence(self):
        import json

        ledger = json.loads((REPO_ROOT / "fork.json").read_text())
        self.assertEqual(ledger["upstream"]["repository"],
                         "https://github.com/disler/super-simple-software-factory")
        self.assertRegex(ledger["upstream"]["reviewed_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(ledger["license"], "MIT")
        self.assertTrue(ledger["divergences"], "a fork with no recorded divergence")
        for item in ledger["divergences"]:
            self.assertEqual(sorted(item), ["id", "summary", "surface",
                                            "upstream_compatible"])


if __name__ == "__main__":
    unittest.main()
