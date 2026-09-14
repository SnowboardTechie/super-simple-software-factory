"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.

A declared path is resolved through `run.targets` before it is looked at, so a
gate can only ever be pointed at the run's own runtime or the codebase it was
given. `../../etc/hosts`, an absolute path elsewhere, and a symlink out of the
tree are all refused by the same resolution rather than by three special cases.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import permissions
from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def _resolved(declared: str, run) -> Path | None:
    """The artifact's real path, or None when it escapes the run's roots."""
    try:
        return run.targets.resolve_artifact(declared)
    except ValueError:
        return None


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    """Every declared artifact is inside the run's roots and really there.

    An empty declaration fails. A gate that examined nothing is not evidence of
    anything, and "I produced no artifact" is the shape of every agent that
    answered in prose and wrote no file — precisely what this gate is for.
    """
    report = GateReport()
    if not envelope.artifacts:
        return report.check("artifacts", False,
                            "no artifact was declared — this phase must produce at least one")
    for a in envelope.artifacts:
        p = _resolved(a, run)
        if p is None:
            report.check(a, False, "declared artifact resolves outside this run's roots")
        else:
            report.check(a, p.exists(),
                         f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _resolved(a, run)
        if p is None or not (p.exists() and p.is_file()):
            continue                       # existence/containment is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _resolved(a, run)
        if p is None or p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def changed_files_match(envelope: EnvelopeBase, run) -> GateReport:
    """The claimed change set and the target repo's actual one must be equal.

    This replaces `diff_matches_claims`, which asked only whether each claimed
    path EXISTS — so claiming `README.md` passed in every repository that has
    one, and a file the agent really did rewrite but never mentioned was
    invisible. Both directions are checked now:

      claimed but unchanged   the envelope is describing work that did not happen
      changed but unclaimed   the envelope is hiding work that did

    "Actual" is what permissions measured this phase change (see
    `permissions.repo_changes`), not the whole dirty tree — so the engineer's
    own uncommitted files are never charged to the agent.
    """
    report = GateReport()
    actual = set(permissions.repo_changes(run))
    claimed: set[str] = set()
    for f in getattr(envelope, "changed_files", []):
        try:
            claimed.add(run.targets.relative_to_worktree(f))
        except ValueError as error:
            report.check(f, False, f"claimed changed file is not in the target repo: {error}")

    for f in sorted(claimed - actual):
        report.check(f, False, "claimed as changed, but the target repo shows no such change")
    for f in sorted(actual - claimed):
        report.check(f, False, "changed in the target repo but missing from the envelope's claim")
    for f in sorted(claimed & actual):
        report.check(f, True, "claimed, and present in the target diff")
    if not claimed and not actual:
        report.check("changed_files", True, "nothing claimed, nothing changed")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        # In the TARGET repo, not wherever the ADW process happens to be — the
        # two are no longer the same directory by construction.
        result = subprocess.run(command, shell=True, cwd=run.repo_root,
                                capture_output=True, text=True)
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
