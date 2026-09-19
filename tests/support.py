"""Shared fixtures: a throwaway git repo, and a Run stand-in the modules accept.

Every module under test takes a `run` and reads a handful of attributes off it.
Building a real Run means a tracer, a sqlite file, and a config on disk — none
of which any of these tests are about. `FakeRun` is the four attributes the
modules actually touch, so a test says what it is testing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADWS = REPO_ROOT / ".claude" / "skills" / "sssf" / "templates" / "adws"
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules.data_types import RunTargets, SSSFConfig  # noqa: E402


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


class GitFixture(unittest.TestCase):
    """A real git repository in a temp dir, with one commit. Removed on teardown.

    Real git, not a mock: everything under test is git's own answer to a
    question, and a mock would only assert that we remembered what we stubbed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # macOS hands out /var symlinks for temp dirs; resolve() up front so the
        # containment assertions compare like with like.
        #
        # The repo is a CHILD of the workspace, never the workspace itself, so
        # sibling repositories and linked worktrees have a managed home. Putting
        # them in the system temp dir instead leaks them between runs, and the
        # second run finds a repo that already has the commit it is about to make.
        self.workspace = Path(self._tmp.name).resolve()
        self.root = self.workspace / "repo"
        self.root.mkdir()
        git("init", "-q", "-b", "main", cwd=self.root)
        git("config", "user.email", "test@example.invalid", cwd=self.root)
        git("config", "user.name", "Test", cwd=self.root)
        git("config", "commit.gpgsign", "false", cwd=self.root)
        self.write("README.md", "initial\n")
        self.write(".gitignore", "ignored/\nsecrets.env\n")
        git("add", "-A", cwd=self.root)
        git("commit", "-qm", "initial", cwd=self.root)

    # ── helpers ─────────────────────────────────────────────────────────────
    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text()

    def commit(self, message: str = "wip") -> str:
        git("add", "-A", cwd=self.root)
        git("commit", "-qm", message, cwd=self.root)
        return git("rev-parse", "HEAD", cwd=self.root)

    def make_repo(self, rel: str) -> Path:
        """A SECOND real git repository, beside the first. Removed with the temp dir.

        Root separation is only testable against separate repositories: the
        whole defect class is "this root is a different git repo and nothing
        was looking at it".
        """
        root = self.workspace / rel
        root.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=root)
        git("config", "user.email", "test@example.invalid", cwd=root)
        git("config", "user.name", "Test", cwd=root)
        git("config", "commit.gpgsign", "false", cwd=root)
        (root / ".gitignore").write_text("ignored/\nsecrets.env\n")
        git("add", "-A", cwd=root)
        git("commit", "-qm", "initial", cwd=root)
        return root.resolve()

    def add_worktree(self, rel: str, branch: str = "review") -> Path:
        """A LINKED worktree of the fixture repo — same git dir, own checkout."""
        worktree = self.workspace / rel
        git("worktree", "add", "-q", "-b", branch, str(worktree), cwd=self.root)
        self.addCleanup(git, "worktree", "remove", "--force", str(worktree), cwd=self.root)
        return worktree.resolve()

    def run_for(self, cfg: SSSFConfig | None = None, *, state_root: Path | None = None,
                control_root: Path | None = None, target_repo: Path | None = None,
                target_worktree: Path | None = None) -> "FakeRun":
        cfg = cfg or SSSFConfig()
        control = control_root or self.root
        targets = RunTargets.resolve(
            cfg,
            control_root=control,
            target_repo=target_repo or self.root,
            target_worktree=target_worktree,
            state_root=state_root or (control / cfg.defaults.data_dir),
        )
        return FakeRun(cfg, targets)


class FakeRun:
    """The slice of Run that permissions/gates/git code actually reads."""

    def __init__(self, cfg: SSSFConfig, targets: RunTargets):
        self.cfg = cfg
        self.targets = targets
        self.tree_baseline: dict[str, str] | None = None

    @property
    def repo_root(self) -> Path:
        return self.targets.target_worktree

    @property
    def session_dir(self) -> Path:
        return self.targets.state_root

    @property
    def context_handoff_dir(self) -> Path:
        return self.targets.state_root / "context_handoff"


def envelope(**fields):
    """A minimal envelope object — gates only read attributes off it."""
    from adw_modules.data_types import GenericOutput

    class _Envelope(GenericOutput):
        changed_files: list[str] = []

    return _Envelope(status=fields.pop("status", "success"), **fields)


def without_env(*names: str):
    """Temporarily clear env vars a test must not inherit."""
    saved = {n: os.environ.pop(n, None) for n in names}

    def restore():
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return restore
