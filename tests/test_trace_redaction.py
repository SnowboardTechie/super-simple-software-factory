"""The trace records what happened, not what it was holding while it happened.

Every tool call's arguments and result land in `events.payload_json` and in
`events.jsonl`, verbatim and unbounded. A `bash` call that exported a token, a
fetch that carried an Authorization header, a `read` of a private file — all of
it was persisted in full, in a sqlite file the visualizer serves.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import ADWS  # noqa: F401  (puts adw_modules on sys.path)

from adw_modules import redact
from adw_modules.data_types import (AgentConfig, EventRecord, GateReport, Phase,
                                    PhaseParams, PromptEngineering)
from adw_modules.tracer import Tracer


class ScrubTest(unittest.TestCase):

    def test_a_secret_named_key_is_replaced(self):
        for key in ("api_key", "API-KEY", "authorization", "password", "secretToken",
                    "AWS_SECRET_ACCESS_KEY", "cookie"):
            with self.subTest(key=key):
                out = redact.scrub({key: "hunter2"})
                self.assertEqual(out[key], redact.PLACEHOLDER)

    def test_an_ordinary_key_survives(self):
        out = redact.scrub({"path": "src/app.py", "exit_code": 0, "sha": "de31374"})
        self.assertEqual(out, {"path": "src/app.py", "exit_code": 0, "sha": "de31374"})

    def test_secret_shaped_values_are_replaced_wherever_they_appear(self):
        for value in ("Authorization: Bearer abc123def456ghi789",
                      "ghp_0123456789abcdefghijABCDEFGHIJ0123",
                      "sk-ant-0123456789abcdefghijklmn",
                      "AKIAIOSFODNN7EXAMPLE",
                      "xoxb-1234-5678-abcdefghijklmnop",
                      "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"):
            with self.subTest(value=value[:24]):
                self.assertIn(redact.PLACEHOLDER, redact.scrub({"note": value})["note"])

    def test_an_environment_secret_is_replaced_by_value(self):
        os.environ["SSSF_TEST_API_KEY"] = "s3cr3t-value-from-the-environment"
        self.addCleanup(os.environ.pop, "SSSF_TEST_API_KEY", None)
        out = redact.scrub({"command": "curl -H x:s3cr3t-value-from-the-environment"})
        self.assertNotIn("s3cr3t-value-from-the-environment", out["command"])

    def test_a_short_environment_value_is_not_used_as_a_needle(self):
        """Redacting on `TOKEN=a` would blank every `a` in the trace."""
        os.environ["SSSF_TEST_TOKEN"] = "ab"
        self.addCleanup(os.environ.pop, "SSSF_TEST_TOKEN", None)
        self.assertEqual(redact.scrub({"note": "a fabulous banana"})["note"],
                         "a fabulous banana")

    def test_long_strings_are_bounded(self):
        out = redact.scrub({"stdout": "x" * 50_000})
        self.assertLess(len(out["stdout"]), redact.MAX_STRING + 200)
        self.assertIn("truncated", out["stdout"])

    def test_long_lists_are_bounded(self):
        out = redact.scrub({"files": [f"f{i}.py" for i in range(500)]})
        self.assertLessEqual(len(out["files"]), redact.MAX_ITEMS + 1)

    def test_deep_nesting_terminates(self):
        payload = current = {}
        for _ in range(60):
            current["next"] = {}
            current = current["next"]
        self.assertIsInstance(json.dumps(redact.scrub(payload)), str)

    def test_nested_structures_are_scrubbed(self):
        out = redact.scrub({"args": [{"env": {"GITHUB_TOKEN": "ghp_xyz"}}]})
        self.assertEqual(out["args"][0]["env"]["GITHUB_TOKEN"], redact.PLACEHOLDER)


class TracerRedactionTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "sssf.db"
        self.jsonl = root / "events.jsonl"
        self.tracer = Tracer(self.db, self.jsonl)

    def test_a_traced_tool_call_is_scrubbed_in_both_sinks(self):
        self.tracer.session_start("a1", "tester")
        self.tracer.event(EventRecord(adw_id="a1", type="tool_call", name="bash",
                                      payload={"args": {"command": "export TOKEN=ghp_0123456789abcdefghijABCDEFGHIJ0123"},
                                               "result": "x" * 40_000}))

        stored = sqlite3.connect(self.db).execute(
            "SELECT payload_json FROM events").fetchone()[0]
        self.assertNotIn("ghp_0123456789", stored)
        self.assertLess(len(stored), 20_000)
        self.assertNotIn("ghp_0123456789", self.jsonl.read_text())

    def test_the_session_request_is_scrubbed(self):
        self.tracer.session_start("a1", "tester")
        self.tracer.session_request("a1", "use api_key=sk-ant-0123456789abcdefghijklmn")
        stored = sqlite3.connect(self.db).execute(
            "SELECT request FROM sessions WHERE adw_id='a1'").fetchone()[0]
        self.assertNotIn("sk-ant-0123456789", stored)

    def test_an_ordinary_event_is_unchanged(self):
        self.tracer.session_start("a1", "tester")
        self.tracer.event(EventRecord(adw_id="a1", type="log", name="commit",
                                      payload={"sha": "de31374", "message": "add health"}))
        stored = json.loads(sqlite3.connect(self.db).execute(
            "SELECT payload_json FROM events").fetchone()[0])
        self.assertEqual(stored, {"sha": "de31374", "message": "add health"})


class EverySinkTest(unittest.TestCase):
    """Insert a secret through each writer, then hunt it in the WHOLE dump.

    The first pass scrubbed `events.payload` and `sessions.request` and called
    it done. The visualizer also reads `events.name`, `envelopes.payload_json`,
    `gate_results.checks_json` and `violations_json`, `processes.command`, and
    `phases.description`/`error` — every one of which took free text straight
    from an agent, a tool call, or an exception message. So the assertion is
    not "this column is clean", it is "the string does not appear anywhere in
    the database or the JSONL".
    """

    SECRET = "ghp_0123456789abcdefghijABCDEFGHIJ0123"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "sssf.db"
        self.jsonl = root / "events.jsonl"
        self.tracer = Tracer(self.db, self.jsonl)
        self.tracer.session_start("a1", "tester")
        self.phase = Phase(phase_id="a1_01_build", adw_id="a1", seq=1,
                           params=PhaseParams(name="build", kind="agent", owner="builder",
                                              description="Implement it"))

    def dump(self) -> str:
        """Every value in every table, as one string. No column left unchecked."""
        conn = sqlite3.connect(self.db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        chunks = []
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                chunks.append(" ".join("" if v is None else str(v) for v in row))
        # The JSONL only exists once an event was written; a sink that writes
        # only to sqlite still has to be searched.
        events = self.jsonl.read_text() if self.jsonl.exists() else ""
        return "\n".join(chunks) + "\n" + events

    def assert_scrubbed(self):
        dump = self.dump()
        self.assertNotIn(self.SECRET, dump)
        self.assertIn(redact.PLACEHOLDER, dump)

    # ── one test per sink ───────────────────────────────────────────────────
    def test_event_name(self):
        self.tracer.event(EventRecord(adw_id="a1", type="tool_call",
                                      name=f"bash: curl -H 'token: {self.SECRET}'"))
        self.assert_scrubbed()

    def test_envelope_payload_json(self):
        self.tracer.envelope_row(self.phase, "builder", "BuildOutput",
                                 json.dumps({"summary": f"used {self.SECRET}"}),
                                 True, 1)
        self.assert_scrubbed()

    def test_envelope_payload_that_is_not_json(self):
        """The malformed-response path stores whatever the agent said."""
        self.tracer.envelope_row(self.phase, "builder", "BuildOutput",
                                 f"not json at all {self.SECRET}", False, 1)
        self.assert_scrubbed()

    def test_gate_checks_and_violations_json(self):
        report = GateReport().check(f"curl -H 'authorization: {self.SECRET}'", False,
                                    f"exit 1\nstderr: bad token {self.SECRET}")
        self.tracer.gate_row(self.phase, "tests_pass", report, 1)
        dump = self.dump()
        self.assertNotIn(self.SECRET, dump)
        # Both columns carry it, and both must be clean.
        conn = sqlite3.connect(self.db)
        checks, violations = conn.execute(
            "SELECT checks_json, violations_json FROM gate_results").fetchone()
        self.assertNotIn(self.SECRET, checks)
        self.assertNotIn(self.SECRET, violations)

    def test_process_command(self):
        self.tracer.process_start("a1", "agent", "builder", 4242,
                                  f"pi --model x --header authorization={self.SECRET}")
        self.assert_scrubbed()

    def test_phase_description_and_error(self):
        phase = Phase(phase_id="a1_02_fix", adw_id="a1", seq=2,
                      params=PhaseParams(name="fix", kind="code", owner="git",
                                         description=f"Retry the push with {self.SECRET}"))
        phase.error = f"remote rejected: token {self.SECRET} is invalid"
        self.tracer.phase_upsert(phase)
        self.assert_scrubbed()

    def test_session_request(self):
        self.tracer.session_request("a1", f"deploy using {self.SECRET}")
        self.assert_scrubbed()

    def test_agent_session_row(self):
        agent = AgentConfig(name="builder", model=f"vendor/model-{self.SECRET}",
                            prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
        self.tracer.agent_session_row("a1", agent, "sssf-a1-builder-0001")
        self.assertNotIn(self.SECRET, self.dump())

    # ── identity must survive redaction ─────────────────────────────────────
    def test_lookup_identity_is_never_corrupted(self):
        """Join keys are exact, or the trace stops being queryable."""
        self.tracer.phase_upsert(self.phase)
        self.tracer.event(EventRecord(adw_id="a1", phase_id=self.phase.phase_id,
                                      type="log", name="paths_touched",
                                      payload={"paths": ["src/app.py"]}))
        self.tracer.envelope_row(self.phase, "builder", "BuildOutput", "{}", True, 1)
        self.tracer.gate_row(self.phase, "artifacts_exist", GateReport(), 1)
        self.tracer.process_start("a1", "adw", "", 99, "adw_build.py")
        conn = sqlite3.connect(self.db)

        for table in ("phases", "events", "envelopes", "gate_results", "processes"):
            ids = conn.execute(f"SELECT adw_id FROM {table}").fetchall()
            self.assertTrue(ids and all(i[0] == "a1" for i in ids), table)
        self.assertEqual(conn.execute(
            "SELECT phase_id FROM envelopes").fetchone()[0], "a1_01_build")
        self.assertEqual(conn.execute(
            "SELECT gate FROM gate_results").fetchone()[0], "artifacts_exist")
        self.assertEqual(conn.execute(
            "SELECT pid FROM processes WHERE pid=99").fetchone()[0], 99)

    def test_an_ordinary_phase_description_survives_verbatim(self):
        self.tracer.phase_upsert(self.phase)
        self.assertEqual(sqlite3.connect(self.db).execute(
            "SELECT description FROM phases").fetchone()[0], "Implement it")


if __name__ == "__main__":
    unittest.main()
