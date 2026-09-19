"""Concrete data types for the SSSF ADW system.

RULE (four-param rule): any function that takes more than 4 parameters takes
ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal, Optional, Type

from pydantic import BaseModel, Field, ValidationInfo, field_validator

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStatus = Literal["queued", "running", "success", "fail"]


# ── Phases ────────────────────────────────────────────────────────────────────

class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str                       # short id, unique within the run: "plan", "build"
    kind: PhaseKind                 # which lane the block renders in
    owner: str                      # engineer's name, "git", or an agent name from config
    description: str                # REQUIRED: what this phase does and why — see below
    retries: int = 0                # agent phases: gate-failure retries via continue

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this "
                f"phase does and why. It is what the trace and the UI show.")
        if text.rstrip(".").casefold() == name.replace("_", " ").casefold():
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name — "
                f"say what it does and why instead.")
        return text


class Phase(BaseModel):
    """The persisted phase record — PhaseParams plus lifecycle."""

    phase_id: str
    adw_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"    # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Envelopes (agent output types) ───────────────────────────────────────────

class EnvelopeBase(BaseModel):
    """Base of every agent's final JSON response. Output types extend this."""

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class GenericOutput(EnvelopeBase):
    pass


class PlanOutput(EnvelopeBase):
    # Subject for committing the PLAN — the spec file the planner wrote, not the
    # implementation it describes. Each agent's commit_message covers its own
    # work product, so a chain that commits per step never reuses one agent's
    # words for another agent's diff.
    commit_message: str = ""


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase


class ScoutFinding(BaseModel):
    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    requirement: str                # the ask, in the requester's words
    met: bool
    evidence: str = ""              # where it lives, or what is missing


class ReviewOutput(EnvelopeBase):
    """Confirmation that what was built is what was asked for — not a test run."""

    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)   # what must change before approval


class DocumentOutput(EnvelopeBase):
    """Where the write-up of a completed change landed."""

    document_path: str = ""         # the doc in the repo, e.g. app_docs/<adw_id>_<slug>.md
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


# ── Deterministic quality blocks ─────────────────────────────────────────────

QualityArea = Literal["frontend", "backend"]
QualityOperation = Literal["lint", "typecheck", "build"]


class QualityCheckSpec(BaseModel):
    """One deterministic quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    argv: list[str]
    timeout_seconds: int = 120


class QualityCheckResult(BaseModel):
    """Captured evidence from one quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ── Change capture (git diff, deterministic) ─────────────────────────────────

class ChangeCapture(BaseModel):
    """Everything documentation.capture() needs. One object, never loose params."""

    base: str = "main"              # the ref the work is measured against
    max_diff_lines: int = 2000      # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the ADW records that choice instead of
    leaving the reader to infer it.
    """

    ref: str                        # what was asked for: "main", or a pinned sha
    commit: str                     # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""                  # `git diff --stat` output, verbatim
    diff_path: str = ""             # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""                  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""             # read this for the full diff


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door a tester agent's report used to —
    the ADW script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


# ── Agent calls ──────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str                       # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    gates: list[Callable] = Field(default_factory=list)   # gate(envelope, run) -> list[str]


# ── Config ───────────────────────────────────────────────────────────────────

class PromptEngineering(BaseModel):
    system: str                     # path to system.md
    user: str                       # path to user.md


class AgentConfig(BaseModel):
    name: str
    coding_agent: Literal["pi", "claude_code"] = "pi"
    model: str = "google/gemini-3.6-flash"
    thinking: str = "medium"        # off | minimal | low | medium | high | xhigh | max
    color: str = ""                 # hex swatch for this agent's lane in the UI
    purpose: str = ""
    prompt_engineering: PromptEngineering
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # allowlist; None = all tools usable
    # What this agent may MODIFY in the repo, enforced in code after every call
    # (see adw_modules/permissions.py). `tools` cannot express this: `bash` runs
    # anything and `write` reaches any path, so an agent's capability list is a
    # statement of intent that nothing checks.
    #   None  -> unrestricted, except the roster-wide `protected_files` paths
    #   []    -> read-only: may modify nothing tracked
    #   [...] -> only these. A trailing "/" means a directory prefix; a "*"
    #            makes it a glob; anything else is an exact path.
    writes: Optional[list[str]] = None


class ConfigDefaults(BaseModel):
    coding_agent: Literal["pi", "claude_code"] = "pi"
    model: str = "google/gemini-3.6-flash"
    thinking: str = "medium"
    color: str = ""
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # roster-wide allowlist; None = all tools usable
    # Off-limits to every agent that has not named them in its own `writes`.
    # The factory's own code is the default: an agent must not be able to edit
    # the machinery that decides whether its work passed.
    protected_files: list[str] = Field(default_factory=lambda: [
        "adws/adw_modules/", "adws/adw_sssf_config/", "adws/adw_*.py",
    ])
    data_dir: str = "adws/adw_data"


class ObservabilityConfig(BaseModel):
    db: str = "adws/adw_data/sssf.db"
    poll_ms: int = 500


class SSSFConfig(BaseModel):
    defaults: ConfigDefaults = Field(default_factory=ConfigDefaults)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    agents: list[AgentConfig] = Field(default_factory=list)


# ── Run roots ────────────────────────────────────────────────────────────────

def _checkout_root(path: Path) -> Path:
    """The canonical worktree root `path` belongs to, or `path` itself outside git.

    This is the identity two roots are compared BY. `<repo>` and `<repo>/src`
    are one checkout; a linked worktree is its own, because git reports it as
    its own toplevel.
    """
    from . import git_helper      # local: git_helper imports nothing back
    return git_helper.repo_root(cwd=path) if git_helper.is_repo(cwd=path) else path


class RunTargets(BaseModel):
    """The four roots a run keeps apart, instead of the one it used to have.

    v1 had a single `repo_root` — the git toplevel of wherever the process
    started — and used it as the factory's home, the runtime's home, and the
    codebase under work all at once. That is fine while a workflow edits the
    repo it was launched from, and impossible the moment one does not: a review
    workflow that reads a checkout elsewhere, a workflow driving a linked
    worktree, a factory installed beside the repositories it serves rather than
    inside one.

        control_root     the factory: ADWs, modules, config. Never the agents'
        state_root       sessions, handoffs, trace. Always writable
        target_repo      the git repository the work is about
        target_worktree  the checkout agents are actually spawned in — the same
                         directory as target_repo, unless a linked worktree was
                         named

    Defaults collapse all four back to the single-root case, so every existing
    ADW behaves exactly as before.
    """

    control_root: Path
    state_root: Path
    target_repo: Path
    target_worktree: Path

    @classmethod
    def resolve(cls, cfg: "SSSFConfig", *, control_root: Path | str | None = None,
                state_root: Path | str | None = None,
                target_repo: Path | str | None = None,
                target_worktree: Path | str | None = None) -> "RunTargets":
        """Build the four roots, resolving symlinks so containment is decidable.

        A root that is a symlink (macOS hands out `/var/...` temp dirs, and a
        submodule-style checkout is often linked) would otherwise never contain
        the `.resolve()`d artifact paths compared against it.

        A named `target_worktree` is CHECKED against `target_repo`, not taken on
        faith. Accepting any directory that happened to be a git repository
        meant the identity the whole run is about could be decided by a typo —
        a workflow pointed at an unrelated checkout would review it happily.
        """
        from . import git_helper      # local: git_helper imports nothing back

        def need(value: Path | str | None, fallback: Path) -> Path:
            path = Path(value).expanduser() if value is not None else fallback
            if not path.exists():
                raise ValueError(f"run target does not exist: {path}")
            return path.resolve()

        # A root that was DEFAULTED from the process directory normalises to the
        # checkout it sits in — v1 read `git rev-parse --show-toplevel` from cwd,
        # so launching an ADW from `src/` has always worked and still does. A
        # root that was NAMED is checked instead of normalised: silently
        # widening someone's explicit `--target src/` to the whole repository is
        # how a run ends up about something other than what it was told.
        control = need(control_root, Path.cwd())
        if control_root is None:
            control = _checkout_root(control)

        repo = need(target_repo, control)
        if target_repo is None:
            repo = _checkout_root(repo)
        elif _checkout_root(repo) != repo:
            raise ValueError(
                f"target repo {repo} is inside a git repository but is not its "
                f"root — that is {_checkout_root(repo)}. Name the checkout root, "
                f"not a path inside it. (A directory outside git is fine: only a "
                f"commit phase needs a repository.)")

        worktree = need(target_worktree, repo)

        if target_worktree is not None and worktree != repo:
            if not git_helper.is_repo(cwd=worktree):
                raise ValueError(
                    f"target worktree {worktree} is not a git repository")
            canonical = git_helper.repo_root(cwd=worktree)
            if canonical != worktree:
                raise ValueError(
                    f"target worktree {worktree} is not a worktree root — its "
                    f"canonical root is {canonical}. Name the root, not a path inside it.")
            if not git_helper.is_repo(cwd=repo):
                raise ValueError(
                    f"target repo {repo} is not a git repository, so {worktree} "
                    f"cannot be one of its worktrees")
            # A linked worktree shares its repository's git common directory.
            # Nothing else does, which makes this the whole membership test.
            if git_helper.common_dir(cwd=worktree) != git_helper.common_dir(cwd=repo):
                raise ValueError(
                    f"target worktree {worktree} belongs to an unrelated repository — "
                    f"it must be {repo} itself or one of its linked worktrees")

        # Relative by default (`adws/adw_data`), and it is the one root that may
        # not exist yet — the first run is what creates it, so the topology is
        # judged on the path rather than on what is currently on disk.
        state = (Path(state_root).expanduser() if state_root is not None
                 else control / cfg.defaults.data_dir).resolve()

        # `permissions.snapshot()` drops every path under the state root, because
        # the runtime is the one place agents must be able to write. A state root
        # that CONTAINS a watched root therefore silences that whole root: every
        # change in it reads as runtime and nothing is ever detected. Inside a
        # watched root (the default) or disjoint from all of them (an external
        # runtime) are both fine; around one is not.
        for label, root in (("control root", control), ("target repo", repo),
                            ("target worktree", worktree)):
            if state == root or state in root.parents:
                raise ValueError(
                    f"state root {state} contains the {label} ({root}). Everything "
                    f"under the state root is treated as this run's own runtime, so "
                    f"that layout would hide every change in it. Put the runtime "
                    f"inside a watched root, or somewhere disjoint from all of them.")

        return cls(
            control_root=control,
            state_root=state,
            target_repo=repo,
            target_worktree=worktree,
        )

    # ── protected roots ─────────────────────────────────────────────────────

    @property
    def protected_roots(self) -> dict[str, Path]:
        """Roots an agent may never modify, keyed by the label used in a snapshot.

        `protected_files` guards the factory's source — but it is matched against
        paths in the tree `permissions.snapshot()` looked at, and that was one
        tree. So separating the roots quietly un-protected them: with a distinct
        `control_root`, rewriting `adws/adw_modules/gates.py` produced no
        detected path at all. The guard was still there, aimed at the wrong
        directory.

        Both extra roots are DENIED outright rather than run through an agent's
        allowlist. `writes:` says what an agent may change in the codebase it was
        given; it was never a licence to edit the factory judging it, or the
        trunk a review worktree was cut from. `state_root` is deliberately absent
        — it is the run's own runtime, and every agent must be able to write it.

        Roots are compared by CANONICAL CHECKOUT, not by path string. With a
        review worktree cut from the repository the factory lives in, `control`
        and `trunk` are one and the same checkout — watching it under both
        labels detected every change twice, reported it twice, and rolled it
        back twice, the second attempt acting on a path the first had already
        deleted. First label wins, so the choice is deterministic.
        """
        seen = {_checkout_root(self.target_worktree)}
        roots: dict[str, Path] = {}
        for label, path in (("control", self.control_root), ("trunk", self.target_repo)):
            root = _checkout_root(path)
            if root in seen:
                continue
            seen.add(root)
            roots[label] = root
        return roots

    # ── runtime locations ───────────────────────────────────────────────────

    def trace_db(self, cfg: "SSSFConfig") -> Path:
        """Where the sqlite trace lives — always inside `state_root`.

        The visualizer derives `sessions/` from the db's own directory, so a db
        that is not beside the sessions sends the UI hunting in the wrong place.
        Resolving a relative path against `control_root` did exactly that the
        moment a state root was separated.

        A relative path is resolved against `state_root`, after stripping a
        leading `data_dir` prefix. That one rule covers both cases: in the
        default layout `adws/adw_data/sssf.db` under a `state_root` of
        `<control>/adws/adw_data` lands on `<control>/adws/adw_data/sssf.db` —
        byte-identical to v1 — while a separated state root gets
        `<state>/sssf.db`. An absolute path is accepted only if it is already
        inside `state_root`, so an explicit override cannot silently escape the
        runtime boundary it is supposed to describe.
        """
        declared = Path(cfg.observability.db).expanduser()
        if declared.is_absolute():
            resolved = declared.resolve()
        else:
            relative = declared
            prefix = Path(cfg.defaults.data_dir)
            if relative.is_relative_to(prefix):
                relative = relative.relative_to(prefix)
            resolved = (self.state_root / relative).resolve()

        root = self.state_root.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"observability.db resolves to {resolved}, outside this run's state "
                f"root ({root}). The visualizer reads sessions/ from the db's own "
                f"directory, so the two must not be separated.")
        return resolved

    def events_jsonl(self, adw_id: str) -> Path:
        """The raw event log for one run — beside its session, under the state root."""
        return self.state_root / "sessions" / adw_id / "events.jsonl"

    @property
    def artifact_roots(self) -> list[Path]:
        """Where a declared artifact is allowed to be: its own runtime, or the
        codebase it was working on. Nothing else is the phase's to point at."""
        return [self.state_root, self.target_worktree]

    def resolve_artifact(self, declared: str) -> Path:
        """Turn a declared artifact path into a real one inside an allowed root.

        Raises ValueError for a blank declaration, a traversal, an absolute path
        elsewhere on the filesystem, and a symlink whose target escapes — the
        last of which is why this resolves before it compares, rather than
        checking the string it was handed.
        """
        text = (declared or "").strip()
        if not text:
            raise ValueError("artifact declaration is empty")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.target_worktree / candidate
        # strict=False: a declared-but-missing artifact must reach the gate that
        # reports it missing, not die here as a traversal.
        resolved = candidate.resolve()
        for root in self.artifact_roots:
            if resolved == root or root in resolved.parents:
                return resolved
        raise ValueError(
            f"artifact {declared!r} resolves to {resolved}, outside this run's roots "
            f"({', '.join(str(r) for r in self.artifact_roots)})")

    def relative_to_worktree(self, declared: str) -> str:
        """A repo path as git spells it, from whatever the agent wrote down.

        Raises ValueError when the path is not inside the target worktree, so a
        claim about `/etc/hosts` fails a changed-file gate instead of being
        compared as a literal string that could never match.
        """
        text = (declared or "").strip()
        if not text:
            raise ValueError("path is empty")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.target_worktree / candidate
        resolved = candidate.resolve()
        if resolved != self.target_worktree and self.target_worktree not in resolved.parents:
            raise ValueError(f"{declared!r} is outside the target worktree "
                             f"({self.target_worktree})")
        return resolved.relative_to(self.target_worktree).as_posix()


# ── Tracing ──────────────────────────────────────────────────────────────────

class EventRecord(BaseModel):
    """One traced event, always logged against adw_id + phase."""

    adw_id: str
    phase_id: str = ""
    type: str                       # phase_start | agent_start | tool_call | handoff | gate_pass | gate_fail | log | agent_end | phase_end | error
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str = ""
    tokens: Optional[int] = None
    # Spans: set both when an event covers real elapsed time (a tool call), so
    # the UI lays it out on a time axis without parsing payload JSON. Left unset,
    # the tracer stamps started_at with the moment the event was recorded.
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Pi coding agent interface ────────────────────────────────────────────────

class PiRequest(BaseModel):
    """Everything one non-interactive pi run needs."""

    prompt: str
    system_prompt: str
    model: str                      # registry pattern, resolved to provider + id
    thinking: str = "medium"
    session_id: str                 # pi --session-id: creates or continues
    session_dir: str
    raw_output_path: str            # JSONL stream lands here
    tools: Optional[list[str]] = None
    extensions: list[str] = Field(default_factory=list)
    cwd: str = "."                  # set from run.repo_root — the codebase root agents work in


class UsageBreakdown(BaseModel):
    """Tokens and the dollars they cost, per component, summed over a call.

    Mirrors pi's `usage` shape one-for-one so the numbers reconcile with what
    pi itself reports: `input` EXCLUDES cache reads, which bill at their own
    (cheaper) rate — add them to learn the size of the prompt that was sent.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Thinking tokens. NOT a fifth component: measured across every session on
    # disk, reasoning is always <= output and the four components above always
    # sum to totalTokens, so reasoning is the thinking SHARE of output, billed
    # at the output rate. Report it nested under output, never added to it.
    reasoning_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    total_cost: float = 0.0

    def add_turn(self, usage: dict, total_tokens: int) -> None:
        """Fold in one pi `message_end` usage object.

        `total_tokens` is passed in rather than re-derived: the caller already
        computes it pi's way (totalTokens, else the sum of the parts).
        """
        cost = usage.get("cost") or {}
        self.input_tokens += usage.get("input") or 0
        self.output_tokens += usage.get("output") or 0
        self.cache_read_tokens += usage.get("cacheRead") or 0
        self.cache_write_tokens += usage.get("cacheWrite") or 0
        self.reasoning_tokens += usage.get("reasoning") or 0
        self.total_tokens += total_tokens
        self.input_cost += cost.get("input") or 0.0
        self.output_cost += cost.get("output") or 0.0
        self.cache_read_cost += cost.get("cacheRead") or 0.0
        self.cache_write_cost += cost.get("cacheWrite") or 0.0
        self.total_cost += cost.get("total") or 0.0

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        for field in self.model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class PiResult(BaseModel):
    text: str = ""
    returncode: int = 0
    session_id: str = ""
    tokens: int = 0
    cost: float = 0.0
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    # Context occupancy after the LAST turn — not a sum. `tokens` bills every
    # turn; this is how full the window is right now, which is what the
    # visualizer's context bar measures against `context_window`.
    context_tokens: int = 0
    context_window: int = 0         # 0 when the registry declares no ceiling
