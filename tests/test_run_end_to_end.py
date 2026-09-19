"""The whole path, with a scripted agent instead of a real one.

Parsing, gates, permissions, commits and tracing are harness code. Proving they
work should cost nothing and never vary, so the coding agent is replaced by a
function that writes exactly the files a scenario needs and returns exactly the
JSON a scenario wants — and every assertion below is about the harness.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from support import GitFixture, git

from adw_modules import agent_pi, agents, gates, git_helper, permissions, session
from adw_modules.data_types import (AgentCall, AgentConfig, BuildOutput, ConfigDefaults,
                                    ObservabilityConfig, PhaseParams, PiResult,
                                    PromptEngineering, RunTargets, SSSFConfig,
                                    UsageBreakdown)


class ScriptedAgent:
    """Stands in for `agent_pi.run`: acts on the tree, then answers."""

    def __init__(self, root: Path, envelope: dict, acts=()):
        self.root, self.envelope, self.acts = root, envelope, list(acts)
        self.calls = 0

    def __call__(self, request, on_event=None, on_spawn=None, on_exit=None):
        self.calls += 1
        if self.calls == 1:
            for rel, text in self.acts:
                target = self.root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if text is None:
                    target.unlink()
                else:
                    target.write_text(text)
        return PiResult(text=json.dumps(self.envelope), session_id="scripted")


class HarnessFixture(GitFixture):

    def setUp(self):
        super().setUp()
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "system.md").write_text("You are a builder.\n")
        (prompts / "user.md").write_text("{{prompt}}\n")
        self.cfg = SSSFConfig(
            defaults=ConfigDefaults(data_dir="adws/adw_data"),
            observability=ObservabilityConfig(db=str(self.root / "adws/adw_data/sssf.db")),
            agents=[AgentConfig(
                name="builder", writes=["src/"],
                prompt_engineering=PromptEngineering(system=str(prompts / "system.md"),
                                                     user=str(prompts / "user.md")))])
        self.write(".gitignore", "ignored/\nsecrets.env\nadws/adw_data/\n")
        self.commit("add runtime gitignore")

    def scripted(self, envelope: dict, acts=()) -> ScriptedAgent:
        fake = ScriptedAgent(self.root, envelope, acts)
        real = agent_pi.run
        agent_pi.run = fake
        self.addCleanup(setattr, agent_pi, "run", real)
        return fake

    def new_run(self):
        return session.ensure(self.cfg, targets=RunTargets.resolve(
            self.cfg, control_root=self.root, target_repo=self.root))

    def build(self, run, gate_list):
        with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                                   description="Implement the request")) as ph:
            return ph.call(AgentCall(output_type=BuildOutput, prompt="add a feature",
                                     gates=gate_list))


class SmokeTest(HarnessFixture):

    def test_a_run_parses_an_envelope_commits_its_paths_and_traces_both(self):
        self.scripted({"status": "success", "summary": "added feature",
                       "changed_files": ["src/feature.py"],
                       "commit_message": "feat: add feature"},
                      acts=[("src/feature.py", "def feature(): ...\n")])
        run = self.new_run()

        build = self.build(run, [gates.changed_files_match])
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Land what the builder changed")) as ph:
            sha = git_helper.commit_paths(build.commit_message, run.take_changes(),
                                          cwd=run.repo_root)
            ph.log(sha=sha)

        self.assertEqual(run.finish(), 0)
        self.assertEqual(build.changed_files, ["src/feature.py"])
        self.assertEqual(git("show", "--pretty=format:", "--name-only", "--no-renames",
                             sha, cwd=self.root).split(), ["src/feature.py"])

        db = sqlite3.connect(self.cfg.observability.db)
        self.assertEqual(db.execute("SELECT status FROM sessions WHERE adw_id=?",
                                    (run.adw_id,)).fetchone()[0], "success")
        types = {r[0] for r in db.execute("SELECT type FROM events WHERE adw_id=?",
                                          (run.adw_id,))}
        self.assertTrue({"phase_start", "agent_start", "handoff", "gate_pass",
                         "phase_end"} <= types, types)
        self.assertTrue(db.execute("SELECT count(*) FROM envelopes WHERE adw_id=? AND valid=1",
                                   (run.adw_id,)).fetchone()[0])


class DirtyTreeTest(HarnessFixture):

    def test_unrelated_uncommitted_bytes_are_preserved_and_never_staged(self):
        self.write("README.md", "the engineer was mid-sentence\n")   # unrelated, dirty
        self.scripted({"status": "success", "summary": "added feature",
                       "changed_files": ["src/feature.py"],
                       "commit_message": "feat: add feature"},
                      acts=[("src/feature.py", "def feature(): ...\n")])
        run = self.new_run()

        build = self.build(run, [gates.changed_files_match])
        sha = git_helper.commit_paths(build.commit_message, run.take_changes(),
                                      cwd=run.repo_root)

        self.assertEqual(git("show", "--pretty=format:", "--name-only", "--no-renames",
                             sha, cwd=self.root).split(), ["src/feature.py"])
        self.assertEqual(self.read("README.md"), "the engineer was mid-sentence\n")
        # Still unstaged and still theirs: modified in the worktree, absent from
        # the index the commit phase just used.
        self.assertIn("README.md", git("diff", "--name-only", cwd=self.root))
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=self.root), "")


class UnauthorizedMutationTest(HarnessFixture):

    def test_a_write_outside_the_allowlist_fails_and_is_undone(self):
        self.write("protected_fixture.txt", "do not touch\n")
        self.commit("add fixture")
        self.scripted({"status": "success", "summary": "helpfully fixed everything",
                       "changed_files": ["src/feature.py"]},
                      acts=[("src/feature.py", "ok\n"),
                            ("protected_fixture.txt", "the agent rewrote this\n")])
        run = self.new_run()

        with self.assertRaises(Exception) as caught:
            self.build(run, [])
        self.assertIn("protected_fixture.txt", str(caught.exception))
        self.assertEqual(self.read("protected_fixture.txt"), "do not touch\n")

    def test_a_gate_failure_without_retries_fails_the_phase(self):
        self.scripted({"status": "success", "summary": "built",
                       "changed_files": []},                      # omits what it wrote
                      acts=[("src/feature.py", "written but unclaimed\n")])
        run = self.new_run()

        with self.assertRaises(agents.GateFailure) as caught:
            self.build(run, [gates.changed_files_match])
        self.assertIn("src/feature.py", str(caught.exception))
        self.assertEqual(run.finish(), 1)

    def test_a_malformed_envelope_fails_after_bounded_retries(self):
        fake = self.scripted({}, acts=[])
        fake.envelope = {"summary": "no status field, so not a BuildOutput"}
        run = self.new_run()

        with self.assertRaises(RuntimeError):
            self.build(run, [])
        self.assertEqual(fake.calls, agents.JSON_FIX_ATTEMPTS + 1)

    def test_the_agent_map_counts_every_send_not_every_phase(self):
        """A retried phase is one phase and two model calls. The map says two.

        This is the number the whole efficiency question rests on: a phase count
        cannot distinguish a clean run from one that paid twice to get the same
        answer, and token totals do not say how many calls produced them.
        """
        replies = ["not JSON at all",
                   json.dumps({"status": "success", "summary": "built",
                               "changed_files": []})]
        sent = []

        def scripted_pi(request, on_event=None, on_spawn=None, on_exit=None):
            sent.append(request.prompt)
            return PiResult(text=replies[min(len(sent), len(replies)) - 1],
                            session_id="scripted",
                            usage=UsageBreakdown(total_tokens=10, total_cost=0.5))

        real = agent_pi.run
        agent_pi.run = scripted_pi
        self.addCleanup(setattr, agent_pi, "run", real)
        run = self.new_run()

        self.build(run, [])

        self.assertEqual(len(sent), 2)                   # the retry really happened
        entry = json.loads((run.session_dir / "agent_map.json").read_text())["builder"]
        self.assertEqual(entry["sends"], 2)
        self.assertEqual(entry["usage"]["total_tokens"], 20)   # both sends, not the last

    def test_a_gate_failure_still_rolls_back_an_unauthorized_write(self):
        self.write("protected.txt", "operator bytes\n")
        self.commit("add protected fixture")
        self.cfg.agents[0].writes = []
        self.scripted({"status": "success", "summary": "omitted my mutation",
                       "changed_files": []},
                      acts=[("protected.txt", "tampered before gate failure\n")])
        run = self.new_run()

        with self.assertRaises(permissions.PermissionBreach):
            self.build(run, [gates.changed_files_match])

        self.assertEqual(self.read("protected.txt"), "operator bytes\n")
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_a_parse_failure_still_rolls_back_an_unauthorized_write(self):
        self.write("protected.txt", "operator bytes\n")
        self.commit("add protected fixture")
        self.cfg.agents[0].writes = []
        fake = self.scripted({"summary": "not a BuildOutput"},
                             acts=[("protected.txt", "tampered before parse failure\n")])
        run = self.new_run()

        with self.assertRaises(permissions.PermissionBreach):
            self.build(run, [])

        self.assertEqual(fake.calls, agents.JSON_FIX_ATTEMPTS + 1)
        self.assertEqual(self.read("protected.txt"), "operator bytes\n")
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_a_clean_self_commit_is_rejected_and_fully_rolled_back(self):
        self.write("protected.txt", "operator bytes\n")
        self.commit("add protected fixture")
        before_head = git("rev-parse", "HEAD", cwd=self.root)
        self.cfg.agents[0].writes = []

        def committing_agent(request, on_event=None, on_spawn=None, on_exit=None):
            self.write("protected.txt", "tampered and committed\n")
            git("add", "protected.txt", cwd=self.root)
            git("commit", "-qm", "agent self commit", cwd=self.root)
            return PiResult(text=json.dumps({
                "status": "success", "summary": "clean tree", "changed_files": []
            }), session_id="scripted")

        real = agent_pi.run
        agent_pi.run = committing_agent
        self.addCleanup(setattr, agent_pi, "run", real)
        run = self.new_run()

        with self.assertRaises(permissions.PermissionBreach):
            self.build(run, [])

        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root), before_head)
        self.assertEqual(self.read("protected.txt"), "operator bytes\n")
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_an_agent_exception_still_rolls_back_an_unauthorized_write(self):
        self.write("protected.txt", "operator bytes\n")
        self.commit("add protected fixture")
        self.cfg.agents[0].writes = []

        def crashing_agent(request, on_event=None, on_spawn=None, on_exit=None):
            self.write("protected.txt", "tampered before crash\n")
            raise RuntimeError("agent process failed")

        real = agent_pi.run
        agent_pi.run = crashing_agent
        self.addCleanup(setattr, agent_pi, "run", real)
        run = self.new_run()

        with self.assertRaises(permissions.PermissionBreach):
            self.build(run, [])

        self.assertEqual(self.read("protected.txt"), "operator bytes\n")
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_an_index_only_mutation_is_rejected_and_rolled_back(self):
        self.cfg.agents[0].writes = []

        def staging_agent(request, on_event=None, on_spawn=None, on_exit=None):
            self.write("staged-by-agent.txt", "not permitted\n")
            git("add", "staged-by-agent.txt", cwd=self.root)
            return PiResult(text=json.dumps({
                "status": "success", "summary": "staged only", "changed_files": []
            }), session_id="scripted")

        real = agent_pi.run
        agent_pi.run = staging_agent
        self.addCleanup(setattr, agent_pi, "run", real)
        run = self.new_run()

        with self.assertRaises(permissions.PermissionBreach):
            self.build(run, [])

        self.assertFalse((self.root / "staged-by-agent.txt").exists())
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=self.root), "")
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")

    def test_index_flags_cannot_hide_an_unauthorized_worktree_change(self):
        self.write("protected.txt", "operator bytes\n")
        self.commit("add protected fixture")
        self.cfg.agents[0].writes = []

        def hiding_agent(request, on_event=None, on_spawn=None, on_exit=None):
            git("update-index", "--assume-unchanged", "protected.txt", cwd=self.root)
            self.write("protected.txt", "hidden tamper\n")
            return PiResult(text=json.dumps({
                "status": "success", "summary": "porcelain is clean", "changed_files": []
            }), session_id="scripted")

        real = agent_pi.run
        agent_pi.run = hiding_agent
        self.addCleanup(setattr, agent_pi, "run", real)
        run = self.new_run()

        with self.assertRaises(permissions.PermissionBreach):
            self.build(run, [])

        self.assertEqual(self.read("protected.txt"), "operator bytes\n")
        flags = git("ls-files", "-v", "protected.txt", cwd=self.root)
        self.assertTrue(flags.startswith("H "), flags)
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "")


class CommitPhaseTest(HarnessFixture):

    def test_a_phase_that_changed_nothing_does_not_commit(self):
        self.scripted({"status": "success", "summary": "nothing to do",
                       "changed_files": []})
        run = self.new_run()
        self.build(run, [gates.changed_files_match])

        with self.assertRaises(RuntimeError):
            git_helper.commit_paths("noop", run.take_changes(), cwd=run.repo_root)

    def test_the_accepted_set_is_drained_between_commits(self):
        """A three-commit chain must not re-offer the first commit's paths."""
        self.scripted({"status": "success", "summary": "built",
                       "changed_files": ["src/feature.py"]},
                      acts=[("src/feature.py", "one\n")])
        run = self.new_run()
        self.build(run, [gates.changed_files_match])

        git_helper.commit_paths("first", run.take_changes(), cwd=run.repo_root)
        self.assertEqual(run.take_changes(), [])


class RelocatedRuntimeTest(HarnessFixture):
    """A separated state root must take the db and the sessions with it."""

    def test_the_db_and_the_sessions_land_together_outside_the_control_root(self):
        state = self.workspace / "runtime"
        state.mkdir()
        self.cfg.observability.db = "adws/adw_data/sssf.db"     # the shipped default
        self.scripted({"status": "success", "summary": "ok", "changed_files": []})

        run = session.ensure(self.cfg, targets=RunTargets.resolve(
            self.cfg, control_root=self.root, target_repo=self.root, state_root=state))
        self.build(run, [])

        self.assertEqual(run.trace_db, state.resolve() / "sssf.db")
        self.assertTrue(run.trace_db.is_file())
        # What the visualizer derives: sessions/ beside the db it was handed.
        self.assertTrue((run.trace_db.parent / "sessions" / run.adw_id).is_dir())
        self.assertTrue((run.trace_db.parent / "sessions" / run.adw_id /
                         "events.jsonl").is_file())
        # And nothing was written into the control root.
        self.assertFalse((self.root / "adws" / "adw_data" / "sssf.db").exists())

    def test_a_db_pointed_outside_the_state_root_refuses_to_start(self):
        state = self.workspace / "runtime2"
        state.mkdir()
        self.cfg.observability.db = str(self.workspace / "escaped.db")

        with self.assertRaises(ValueError):
            session.ensure(self.cfg, targets=RunTargets.resolve(
                self.cfg, control_root=self.root, target_repo=self.root,
                state_root=state))
        self.assertFalse((self.workspace / "escaped.db").exists())


class ProtectedRootRunTest(HarnessFixture):
    """The whole path, with the factory living in its own repository."""

    def test_an_agent_rewriting_the_factory_fails_the_phase_and_is_undone(self):
        control = self.make_repo("factory")
        (control / "adws" / "adw_modules").mkdir(parents=True)
        (control / "adws" / "adw_modules" / "gates.py").write_text("def strict(): ...\n")
        git("add", "-A", cwd=control)
        git("commit", "-qm", "factory", cwd=control)

        fake = self.scripted({"status": "success", "summary": "improved the gates",
                              "changed_files": ["src/feature.py"]},
                             acts=[("src/feature.py", "real work\n")])
        # The second act lands in the factory repo, not the target.
        original = (control / "adws" / "adw_modules" / "gates.py").read_text()
        real_call = fake.__call__

        def tamper(*args, **kwargs):
            result = real_call(*args, **kwargs)
            (control / "adws" / "adw_modules" / "gates.py").write_text("def pass_everything(): ...\n")
            return result
        agent_pi.run = tamper

        self.cfg.observability.db = "adws/adw_data/sssf.db"   # relative: follows state_root
        run = session.ensure(self.cfg, targets=RunTargets.resolve(
            self.cfg, control_root=control, target_repo=self.root,
            state_root=control / "adws" / "adw_data"))

        with self.assertRaises(Exception) as caught:
            self.build(run, [])
        self.assertIn("gates.py", str(caught.exception))
        self.assertEqual((control / "adws" / "adw_modules" / "gates.py").read_text(),
                         original)


class RuntimeStateTest(HarnessFixture):

    def test_the_session_runtime_lands_under_the_state_root(self):
        self.scripted({"status": "success", "summary": "ok", "changed_files": []})
        run = self.new_run()
        self.build(run, [])

        self.assertEqual(run.targets.state_root, self.root / "adws" / "adw_data")
        self.assertTrue((run.session_dir / "builder" / "envelope.json").is_file())
        self.assertTrue(run.context_handoff_dir.is_dir())
        # Everything the run wrote is inside its own state root, not the repo.
        self.assertNotIn("src/", git("status", "--porcelain", cwd=self.root))


if __name__ == "__main__":
    unittest.main()
