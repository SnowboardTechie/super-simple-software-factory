# Super Simple Software Factory

> **Repeatable agents-plus-code workflows, packaged as one skill, stamped into any repo.**
> Deterministic Python owns the graph. Coding agents are bounded nodes inside it.

📺 Full breakdown on YouTube: **[Super Simple Software Factory](https://youtu.be/haUfb1ievTE)**

<p align="center">
  <img src="images/00_swimlane_waterfall.svg" alt="A run as swim lanes: engineer, code, planner, builder, and reviewer phases laid on a time axis, each block labelled with its duration, one phase still running and the next still queued" width="850">
</p>

<p align="center">
  <img src="images/01_factory_spine.svg" alt="A run spine: engineer, agent, and code phases on a deterministic rail, every event dropping into a SQLite trace db that the UI polls" width="850">
</p>

A software factory does one thing: it gives you more leverage on your prompt. How much leverage depends entirely on what you invest in it. At the low end you chain two agents together and hope. At the high end you build a system of agents plus code that runs without you, and does the job about as well as you would.

Everyone can get an agent to write code once. Almost nobody gets the same result twice. This fixes that by moving the control plane out of the prompt and into Python. An ADW script (AI Developer Workflow) owns sequencing, retries, and acceptance. Agents work inside named phases. Typed JSON envelopes carry context across the seams. Every event streams into SQLite while it is still happening. **Agent proposes, code disposes.**

> [!NOTE]
> **This branch is the skill alone**, which is the thing you install. For a repo with the factory already stamped into it, a demo app it planned, built, tested, reviewed, and documented, and the real traces from those runs, see the **[`example` branch](../../tree/example)**.

---

## This is a fork

Forked from **[disler/super-simple-software-factory](https://github.com/disler/super-simple-software-factory)** at `de31374`, MIT, attribution and `LICENSE` unchanged. Upstream is tracked normally; this fork is never rebased or force-pushed.

The changes here are all one argument: **an agent's blast radius should be a thing the harness can state, and then check.** Upstream verifies an agent's claims after the fact, which is the right design — these changes make the verification actually load-bearing, because a check that can be passed by accident is not a check.

```
uv run tests/run_tests.py          # the whole suite, no global install
uv run tests/run_tests.py gates    # one file
```

`fork.json` is the same list in machine-readable form, for anything downstream that pins an exact engine commit.

### Divergence from upstream

Four of these change a generic interface. They are **not drop-in compatible**, and an ADW carried over from upstream needs the migration in the right-hand column.

| What changed | Why | Migration |
|---|---|---|
| **`commit_all()` is gone.** `commit_paths(message, paths, cwd=...)` stages an explicit path set | `git add -A` swept the engineer's unrelated uncommitted work into the agent's commit, under the agent's message | `ph.log(sha=git_helper.commit_paths(message, run.take_changes(), cwd=run.repo_root))`. There is deliberately no shim: a silent `git add -A` behind a new name would be the same bug with a better name |
| **`diff_matches_claims` → `changed_files_match`** | The old gate asked only whether each claimed path exists, so claiming `README.md` passed anywhere, and a file the agent rewrote but never mentioned was invisible | Swap the name at the call site. Expect it to be stricter: an omission fails now, not just an invention |
| **Artifacts resolve under explicit roots**, and an empty `artifacts` list fails `artifacts_exist` | `Path(a)` reached anywhere the process could; a gate that examined nothing reported green | Declare artifacts inside `context_handoff_dir` or the target repo. An agent that writes no file now fails the gate that says it must |
| **`run.repo_root` is derived, not assigned** — a run carries `control_root`, `state_root`, `target_repo`, `target_worktree` (`RunTargets`) | One root meant the factory's home, the runtime's home, and the codebase under work were the same directory by construction | Nothing, for an ADW that works on the repo it was launched in — the defaults collapse to exactly that. Pass `session.ensure(cfg, adw_id, targets=...)` to point a run elsewhere |
| **`observability.db` is resolved against `state_root`**, not the process directory, and an absolute path outside `state_root` is refused | The visualizer derives `sessions/` from the db's own directory, so a db that is not beside its sessions points the UI at nothing | Nothing for the shipped default — `adws/adw_data/sssf.db` under the default state root resolves to exactly the same file as v1. An explicit absolute db must be inside `state_root` |

And three that are additive — existing callers are unaffected:

| What changed | Why |
|---|---|
| `permissions.snapshot()` fingerprints by **type + git object id**, watches gitignored files and tracked paths hidden by index flags, and covers **every watched root** | Numstat counts cannot tell two different `+3/−3` rewrites apart, content alone cannot see a `chmod +x`, and assume-unchanged/skip-worktree can make porcelain omit a modified file entirely |
| Agent phases snapshot and restore **HEAD, refs, and the exact index bytes**, and enforce permissions on success, parse/gate failure, or agent-process failure | A clean self-commit is invisible to worktree status; index flags can hide worktree changes; and the old success-only check left unauthorized bytes behind whenever parsing or a gate raised |
| `control_root` and `target_repo` are **denied mutation roots** whenever they differ from `target_worktree` | `protected_files` was matched against the one tree `snapshot()` looked at, so separating the roots aimed the factory's own guard at the wrong directory |
| A named `target_worktree` must be `target_repo` itself or one of its **linked worktrees**, and a named `target_repo` must be a **checkout root** | Any directory that happened to be a git repo was accepted, so the identity a whole run is about could be decided by a typo. A defaulted root still normalises to its checkout root, so launching from `src/` works as it always did |
| Build-noise (`__pycache__`, `*.pyc`) is exempt in the target worktree only | In the factory or the trunk a `.pyc` is executable Python in a tree no agent may write to |
| Every `git_helper` function takes an explicit `cwd`, defaulting to the process directory | A run can now be about a repository it was not launched from |
| `redact.py`, applied at **every** tracer sink | `events.name`/`payload_json`, `envelopes.payload_json`, `gate_results.checks_json`/`violations_json`, `processes.command`, `phases.description`/`error`, `sessions.request` and `agent_sessions.model` all took free text straight from an agent, a tool call, or an exception message, and the visualizer reads all of them |

### What this still is not

**Not a sandbox.** Every one of these checks runs *after* the agent's turn, against the repository. `bash` can still do anything the operator can — reach the network, write outside the repo, touch another checkout entirely. What the harness guarantees is that a change *inside the target worktree* is detected, named, and undone where it can be, and that the phase fails either way. A workflow that needs a stronger guarantee than that should be read-only.

Four limits worth naming rather than discovering:

- git does not walk into a **wholly-ignored directory** — it reports `node_modules/` as one entry — so a change to a file inside one is not fingerprinted. The directory appearing or disappearing is.
- **Only roots are watched.** `control_root`, `target_repo` and `target_worktree` are fingerprinted; a write to any *other* directory on the machine is not seen at all. Roots that are not git repositories are skipped entirely — there is nothing to diff against. Roots are compared by canonical checkout, so a factory and a trunk that are the same checkout are watched once, under `control`.
- **Runtime exemption is path-exact.** Only the resolved `state_root` is omitted from snapshots. A same-named `adws/adw_data/` directory in a separate target repository receives no special permission.
- **Git state is restored locally, not remotely.** Agent phases may not change HEAD, refs, or the index; those surfaces and safely recoverable worktree bytes are rolled back before the phase fails. Dangling objects and reflog entries may remain for recovery; a command that already pushed, rewrote an external repository, or changed server state cannot be recalled.
- **The state root may not contain a watched root.** Everything under it counts as the run's own runtime, so a state root wrapped *around* a watched root would hide every change in it. Inside a watched root (the default) or disjoint from all of them is supported; `RunTargets.resolve` refuses the rest.
- **Redaction is pattern-based.** A secret with no recognisable name and no recognisable shape passes through. Files under the session runtime (`raw_output.jsonl`, `envelope.json`, rendered prompts) are deliberately raw — they are the evidence the trace *refers to*. Trace a reference to one, rather than copying a private payload into the db.
- **Detection is not prevention.** Everything here runs after the agent's turn. What can be undone is undone and the phase fails either way, but a `bash` call that already sent data somewhere cannot be recalled.

---

## Why this exists

<p align="center">
  <img src="images/02_control_plane.svg" alt="Left: one big agent owning its own loop with no phase boundary and no acceptance. Right: code owning the loop with agents as bounded, gated nodes" width="780">
</p>

Hand a capable model your whole SDLC and you get a machine with no seams. There is no phase boundary, so you cannot say which step failed. There is no acceptance criterion you can name, so "done" means "the agent stopped talking." A retry is a cold start that throws away everything the agent just learned. The only trace is a transcript you have to read like a novel. Run it twice, get two different systems.

The fix is not a better prompt. The fix is deciding, deliberately, that **code owns sequencing, retries, and acceptance, and the agent owns only the work inside one bounded phase**. Everything else falls out of that one line. Phases become the unit of the trace. Envelopes become the only way context crosses a seam. Gates become the definition of done. A correction becomes cheaper than a restart, because the session is still alive.

### Agents are great. You do not always need one.

This is the part most engineers are going to skip, and pay for later.

Code costs nothing. It runs at the speed of light. You can change it in a second. And you actually own it, which is not true of any model you are renting by the token.

So when the invocation is already known, write it down. `bun test` is not a judgement call. Neither is `ruff check`. An agent rediscovering your test runner burns a context window to learn what a subprocess already knows, and it charges you for the privilege every single run. Worse, it puts a passing test suite into a context window, which buys you nothing at all.

Agents are for the parts that need reading and deciding. Everything else is a `kind="code"` phase. When code fails, the failure comes back to the builder as an envelope, through the same door an agent's report would have used. The repair loop is identical. You just stopped paying an agent to do arithmetic.

The bill for skipping this is not only tokens. It is cost, speed, and consistency, and you pay it on run one hundred and run one thousand, not on run one.

> *Same models. Same prompts. The difference is who owns the loop.*

---

## Install

Two steps: get the skill into your repo, then stamp the factory.

### Agentic Install

Copy `.claude/skills/sssf/` into the target repo and type `/sssf install` inside Claude Code. The skill is named `sssf`, so that is the skill name followed by the `install` argument. There is no bare `/install` command. The agent reads the skill's own `cookbooks/install.md` and does the rest.

### Manual Install

**Prereqs:** [`uv`](https://docs.astral.sh/uv/), [`pi`](https://github.com/mariozechner/pi-coding-agent), `sqlite3`, and an API key for whichever providers your roster names (see below). [`bun`](https://bun.sh) only if you want the visualizer.

```bash
# 1. get the skill into the target repo
mkdir -p .claude/skills
cp -r /path/to/super-simple-software-factory/.claude/skills/sssf .claude/skills/

# 2. stamp the factory (run from the target repo ROOT, the cwd is where everything lands)
uv run .claude/skills/sssf/scripts/install.py
cp .env.sample .env                              # then set OPENROUTER_API_KEY
pi --version                                     # confirm pi is on PATH, or set PI_PATH in .env
git init && git commit --allow-empty -m init     # chains that end in a commit phase need a repo

# 3. smoke test: two cheap read-only runs, end to end
just demo
just sessions              # what just happened
just obs                   # the trace UI, needs bun

# no just? every recipe is one line. the raw form of `just demo` is:
uv run adws/adw_prompt.py "reply with a one-line summary of this repo" --agent scout
```

Re-running `install.py` is safe. It skips every file that already exists and reports what it skipped, so a second run doubles as a drift check. `--force` refreshes stamped code to the skill's current version, but it overwrites **all** stamped files including your `sssf.config.yaml` and your prompts, so commit first.

Green on the smoke test means the whole path works: config validated, session minted, Pi ran, envelope parsed, events landed in `adws/adw_data/sssf.db`. Fix it there before composing anything larger, because every multi-agent chain rides this exact path.

### Which API keys you actually need

That depends on your roster, not on this repo. Every `model:` in `sssf.config.yaml` is written `provider/model-id`, and the provider half decides the key. Which key pi reads for a given provider comes from `~/.pi/agent/models.json`.

The starter roster deliberately mixes providers to show the point, so out of the box it wants three:

| Model in the starter roster | Provider | Key |
|---|---|---|
| `google/gemini-3.6-flash` (default, builder, scout) | served via openrouter | `OPENROUTER_API_KEY` |
| `fireworks/accounts/fireworks/models/kimi-k3` (planner) | fireworks | `FIREWORKS_API_KEY` |
| `openai/gpt-5.6-terra`, `openai/gpt-5.6-luna` (reviewer, documenter) | openai | `OPENAI_API_KEY` |

**Want one key instead of three?** Delete the per-agent `model:` lines and let every agent inherit `defaults.model`. The whole roster then runs on one provider. Cheapest way to get a first green run.

One sharp edge worth knowing: `agents.validate()` checks that a model is *written* as `provider/id`, not that the provider is reachable or that its key is set. A missing key does not fail at startup. It fails when that agent runs, partway into a chain.


---

## Three principles

Everything here is built to be **observable**, **customizable**, and **reusable**. Those are not adjectives, they are the reason the parts are shaped the way they are.

**Observable.** If you cannot measure your agents, you cannot improve them. Every event goes into SQLite as it happens, so you can watch a run mid-flight, not read about it afterwards.

**Customizable.** One YAML file sets the core four for every agent: context, model, prompt, tools. Different models at different price and speed points, in the same run. It is not about which model is best anymore, it is about which model is right for that one phase.

**Reusable.** The whole thing is a skill you stamp into any repo, then bend to fit. The tests it ships are not your tests. The prompts it ships are starters. It is designed to be edited.

There are three actors here, and the design keeps them separate on purpose: **the engineer**, **the code**, and **the agents**. The trick is not running more agents. The trick is using all three at the right moment.

---

## The skill is the product

<p align="center">
  <img src="images/03_skill_stamp.svg" alt="The sssf skill directory on the left stamping config, adws, and prompt_engineering into three different target repos" width="780">
</p>

Everything lives in `.claude/skills/sssf/`. `SKILL.md` carries the hard rules and routes each request to one of nine cookbooks. `references/` holds the deep specs, `scripts/` holds the generators, `templates/` holds exactly what gets stamped.

| What lands in your repo | Where it comes from | Tracked |
|---|---|---|
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` | yes, it is your agent roster |
| `adws/adw_*.py` | `templates/adws/` | yes, twelve starter workflows |
| `adws/adw_modules/` | `templates/adws/adw_modules/` | yes, all low-level logic |
| `adws/adw_data/prompt_engineering/` | `templates/prompt_engineering/` | yes, **your prompts live here** |
| `adws/adw_data/harness_engineering/` | `templates/harness_engineering/` | yes, pi extensions |
| `.env.sample` | `templates/env.sample` | yes |
| `justfile` | `templates/justfile` | yes, starter recipes to run and watch |
| `adws/adw_data/sessions/`, `sssf.db` | created at runtime | no, gitignored |

The prompts are yours the moment they land. Edit them in `adws/adw_data/prompt_engineering/{agent}/`, never back inside the skill.

There is no DSL here. No framework to learn. It is Python, YAML, agents, and a skill, which is exactly what these models are already trained on. Staying in distribution is a feature.

---

## The agent roster

`adws/adw_sssf_config/sssf.config.yaml` answers one question per entry: who is this agent. One agent, one prompt, one purpose.

```yaml
defaults:
  coding_agent: pi                 # v1 runs pi only, claude_code is schema-valid and stubbed
  model: google/gemini-3.6-flash   # provider/model-id, a bare id can match several providers
  thinking: medium                 # off | minimal | low | medium | high | xhigh | max
  protected_files:                 # no agent may edit the machinery that grades it
    - adws/adw_modules/
    - adws/adw_sssf_config/
    - adws/adw_*.py
  data_dir: adws/adw_data

agents:
  - name: planner
    model: fireworks/accounts/fireworks/models/kimi-k3
    thinking: high                 # per-agent overrides win over defaults
    color: "#a78bfa"               # this agent's lane swatch in the trace
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    harness_engineering:
      - adws/adw_data/harness_engineering/subagents.ts   # this agent can spawn subagents
    writes:                        # the plan is all it may leave in the repo
      - specs/
```

Five starter agents ship in the box: `planner`, `builder`, `scout` (read-only recon), `reviewer`, and `documenter`. There is no tester, because running a suite is a known command and therefore code.

Every agent gets its own model, thinking level, prompts, tools, and harness. That is the core four, and it is the whole surface you tune. Give the planner a frontier model and the builder a cheap fast one. Give the scout subagents. Give the reviewer no ability to write code at all.

**`tools` is a capability list. `writes` is the boundary.** They are not the same thing, and the difference matters: `bash` runs anything, including `git checkout`, and `write` reaches any path. So "this agent changes nothing" is enforced in code, after every call, by comparing the repo before and after. Unauthorized changes are rolled back and the phase fails. A read-only agent is read-only with respect to your repo, never unable to write its own report.

Config defines who an agent **is**. The ADW call site defines how it is **used**. That split is what lets one agent serve many different calls. **ADW scripts never name a model, they name an agent.**

---

## Phases: three lanes, one primitive

<p align="center">
  <img src="images/04_phase_lanes.svg" alt="Swim lanes for engineer, git, planner, builder, and reviewer with phase blocks placed on a time axis and one dashed queued block" width="780">
</p>

Every run is a sequence of phases, and every phase is the same context manager no matter who owns it.

```python
REQUIRED_AGENTS = ["planner", "builder", "reviewer"]   # names, never models

cfg = agents.load_config(config)
agents.validate(cfg, REQUIRED_AGENTS)   # a missing agent fails before anything spawns
run = session.ensure(cfg, adw_id)       # pin-or-create the session

with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                           description="Turn the request into an implementable plan")) as ph:
    plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                             gates=[gates.artifacts_exist, gates.files_non_empty]))

with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                           description="Land the builder's changes")) as ph:
    message = build.commit_message or f"sssf({run.adw_id}): {build.summary}"
    ph.log(sha=git_helper.commit_paths(message, run.take_changes(),
                                       cwd=run.repo_root), message=message)

return run.finish(accepted=review.approved, reason="the reviewer never approved")
```

Three kinds, three swim lanes. **engineer** is the human lane. **agent** is `ph.call(...)`: prompt in, typed envelope out, gates verified. **code** is a deterministic step that stands on its own, like a commit or a migration, and it is never buried inside an agent phase, so the trace shows exactly when code ran and when an agent was working.

That commit phase is the whole pattern in miniature. The builder proposes the message as a field on its envelope. Code decides whether to use it, falls back when it is empty, and performs the write. The agent never runs `git commit` itself.

It also decides *which paths*. `run.take_changes()` is the accepted change set — the paths `permissions.enforce` watched the agents actually change and allowed — and `commit_paths` stages exactly those and refuses anything else. The agent's `changed_files` claim is gated, but it never reaches git.

**Success must be earned.** Every phase defaults to `fail`. A clean exit flips it, and an agent phase also needs its envelope to parse and every gate to come back green. `run.finish(accepted=...)` adds the second question, because phases passing is not the same as the run being acceptable: a test phase that ran a red suite did its job perfectly. One call settles the exit code, the session status, and the banner together, so they cannot disagree.

---

## Envelopes and gates

<p align="center">
  <img src="images/05_envelope_gates.svg" alt="An agent's final JSON parsed against its output type, checked by gates, with violations looping back into the same session as a correction" width="780">
</p>

An agent has exactly two output channels: reference files written into `context_handoff/`, and a final valid-JSON response parsed against the output type the call declared. Code persists that response as `envelope.json`, records it, and injects it into the next agent's prompt. Context transfers in code, not in conversation.

```python
class EnvelopeBase(BaseModel):
    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""

class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase
```

Determinism is wired into every step. Agents must return a specific structure, every time. If it does not parse, they get asked again until it does.

Gates verify claims, never predictions. Nobody knows which files an agent will touch before it finishes, so gates run **after** the fact against the envelope's own declarations: `artifacts_exist`, `files_non_empty`, `json_parses`, `changed_files_match`, `tests_pass(...)`. A gate is a callable with the signature `gate(envelope, run) -> GateReport`, one `check(item, ok, note)` per thing it examined, so a green gate tells you *what* it verified.

When JSON does not parse or a gate returns violations, **nothing restarts**. The harness re-prompts the same session with a correction naming exactly what was wrong, and the context window stays intact. Pi treats `--session-id` as create-or-continue, so running an agent and continuing it are the same call. A cold restart throws away everything the agent learned. A correction costs one message.

The output contract lives in three places and they are one thing: the type in `data_types.py`, the JSON example in that agent's `user.md` `## Report` section, and `output_type=` at the call site. **Change one, change all three in the same edit.**

---

## The trace

<p align="center">
  <img src="images/06_trace_path.svg" alt="Running agents to tracer.py to a WAL SQLite db with seven tables, read by a cursor poll query, with no websocket and no ingest endpoint" width="780">
</p>

One data path, no exceptions: **agents write to SQLite, readers poll SQLite.** `agent_pi.py` tails the coding agent's JSONL stdout line by line and the tracer inserts each event while the agent is still working, so tool calls are visible mid-run instead of batched at the end.

Ten event types land across seven tables: `sessions`, `phases`, `events`, `envelopes`, `gate_results`, `agent_sessions`, and `processes` (adw_id to pid, so a stuck run can be found and stopped). Every event logs against both its `adw_id` and its `phase_id`, and `parent_id` nests spans, so an agent phase expands into its own tool calls.

Pi announces a tool call across three raw events, so the interface folds them into exactly **one** `tool_call` row per real call. Each row is named the way you would read it aloud (`bash: ls -la src`) and carries `{tool, tool_call_id, args, result_snippet, ok, duration_ms, agent}`.

```sql
select * from events where adw_id = ? and rowid > ? order by rowid limit 500;
```

That one cursor query is the entire transport. Live view and full history are the same query at different cadence, which is why there is no ingest endpoint, no WebSocket, no backfill, and no separate replay path. Every connection opens WAL, so reads never block the running writers.

Files stay the raw record (`raw_output.jsonl`, `envelope.json`, `agent_map.json`). The db is the queryable mirror. Losing it loses nothing you cannot rebuild.

The skill ships a read-only UI for this db at `.claude/skills/sssf/apps/visualizer/`: Vue and Vite served by Bun on port 4600, with sessions, a trace waterfall, and per-phase tool-call detail.

```bash
cd .claude/skills/sssf/apps/visualizer && bun install
SSSF_DB=/abs/path/to/your-repo/adws/adw_data/sssf.db bun run server/index.ts &
bunx vite
```

It resolves its target through `--db`, then `SSSF_DB`, then `<cwd>/adws/adw_data/sssf.db`, so one instance can point at any stamped repo. Pass the db explicitly, because the server runs from the app dir.

---

## What is in this branch

```
super-simple-software-factory/          # the deployable factory, and nothing else
└── .claude/skills/sssf/
    ├── SKILL.md                        # hard rules + request routing table
    ├── cookbooks/                      # 9 orchestrator playbooks, loaded lazily
    ├── references/                     # config / handoff / observability specs
    ├── scripts/                        # install.py, make_config.py, make_adw.py
    ├── apps/visualizer/                # the read-only trace UI (Vue + Vite on Bun)
    └── templates/                      # EXACTLY what install.py stamps
        ├── sssf.config.yaml            # the starter roster
        ├── prompt_engineering/{agent}/ # system.md + user.md per agent
        ├── harness_engineering/        # pi extensions
        └── adws/
            ├── adw_*.py                # the twelve starter workflows
            └── adw_modules/            # ALL low-level logic, ADW scripts stay thin
```

The skill is also what an agent reads to *operate* the factory. `SKILL.md` is the central idea, and the cookbooks are lazily loaded recipes it pulls in one at a time: set up the factory, create an ADW, modify a chain, add an agent, run and monitor. If you can teach an agent to do something, teach it, then go build the thing it cannot.

---

## The twelve starter workflows

Every ADW takes the same shape:

```bash
uv run adws/adw_*.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
```

| ADW | Chain | Reach for it when |
|---|---|---|
| `adw_prompt` | engineer to \<agent\> | one agent, one prompt, `--agent NAME` picks who |
| `adw_scout` | engineer to scout | read-only recon, nothing changes |
| `adw_plan` | engineer to planner | you want the spec before any code |
| `adw_build` | engineer to builder | the plan already exists |
| `adw_quality` | engineer to code(quality) | lint, typecheck, build, no agents at all |
| `adw_plan_build` | planner, builder, git(commit) | small, well-understood work |
| `adw_build_test` | builder, code(test), bounded fix loop | there is a suite to satisfy |
| `adw_build_review` | builder, reviewer, bounded revise loop | "is this what was asked for" matters more than "does it run" |
| `adw_plan_build_test` | plan, build, code(test), git(commit) | the standard chain |
| `adw_plan_build_test_quality` | same, plus lint/typecheck/build gates | the repo has quality commands worth enforcing |
| `adw_document` | code(git diff), documenter | write up what just shipped |
| `adw_simple_sdlc` | plan, build, test, review, document | the work is real and its shape is not obvious |

`adw_simple_sdlc` lands three commits from three authors. The plan, the code, and the write-up each get their own, and each message is the words of the agent that produced it.

`--adw-id` is optional everywhere. Omit it and a fresh id is minted and printed. Supply it and the run joins that session: same dirs, same `context_handoff/`, and each agent **resumes its existing context window** through `agent_map.json` instead of starting cold. That is how you chain workflows.

```bash
uv run adws/adw_plan.py "add a /health endpoint"              # prints adw_id a1b2c3d4
uv run adws/adw_build_test.py "implement the plan" --adw-id a1b2c3d4
```

Watch a run with the trace db directly:

```bash
sqlite3 adws/adw_data/sssf.db "select adw_id, status, substr(request,1,60), total_tokens from sessions order by started_at desc limit 10;"
sqlite3 adws/adw_data/sssf.db "select seq, name, kind, owner, status from phases where adw_id='a1b2c3d4' order by seq;"
sqlite3 adws/adw_data/sssf.db "select kind, name, pid, command from processes where adw_id='a1b2c3d4' and ended_at is null;"
```

Reads never block a running workflow, the db is WAL. `install.py` stamps a `justfile` wrapping all of the above, so in a fresh repo these are `just sessions`, `just phases <adw_id>`, `just tail <adw_id>`, and `just procs <adw_id>`.

---

## Where it can still fail

Honest edges, because knowing them is cheaper than discovering them.

| Failure | What actually happens | What to do |
|---|---|---|
| The test phase reports green on a fresh install | `quality.py` ships placeholder commands that exit 0. Three ADWs run them as their test phase | Wire your real commands into `quality.py` before trusting `adw_build_test`, `adw_plan_build_test`, or `adw_simple_sdlc`. This is the first thing to customize |
| A bare model pattern | The same model sits under several providers, so `gemini-3.6-flash` matches three catalog entries and `agents.validate()` refuses to spawn | Always write `provider/model-id` |
| `just` is not installed | The stamped `justfile` is a convenience wrapper, nothing depends on it | Every recipe is a one-line `uv run` or `sqlite3` command. Open the justfile and run the line yourself |
| A coding agent hangs silently | No events, no tokens, an empty `raw_output.jsonl`. The trace goes quiet rather than red | Query `processes` for what is alive and kill it children-first. A killed run finalizes its own trace to `fail` |
| The synced triad drifts | Type, `## Report` example, and `output_type=` disagree, so every call burns correction rounds | Grep the type name and fix all three in one edit |
| Gates pass, output is bad | Gates check what a predicate can check, not plan quality or code taste | Run the `reviewer`, or read it yourself |
| An agent edits something it should not | Detected and rolled back after the call, and the phase fails | Expected. Widen that agent's `writes` if the change was legitimate |
| An agent stages or commits during its phase | HEAD, refs, or index drift is detected; local Git state and safely recoverable bytes are restored, and the phase fails | Keep Git mutation in an explicit code phase. Agent phases only edit permitted worktree paths |
| Commit phase has nothing to commit | `commit_paths` raises when the target is not a git repo, when the accepted change set is empty, or when a path it was handed is not actually dirty | `git init` with one commit first. A no-op build fails the phase rather than committing nothing |
| A changed-file gate fails on a file the agent did not mention | `changed_files_match` compares the claim against the phase's real diff in **both** directions | Expected. The envelope must name every path the agent changed, and no path it did not |
| A declared artifact is refused | Artifacts resolve under the run's state root or target worktree; traversal, absolute paths elsewhere, and symlink escapes are rejected | Write reports into `context_handoff_dir` or the repo, and declare them by the path you wrote |
| `install.py --force` | Overwrites **all** stamped files, config and prompts included | Commit before you force |
| `coding_agent: claude_code` | Schema-valid, but `agent_cc.py` raises | v1 is Pi only |

Also missing on purpose, so you know what to add: this runs on your current branch. For real work you want a branch per run, a sandbox around the agent, and a merge step at the end.

**Is this overkill for a one-off feature?** Yes. Prompt an agent and move on. This earns its keep when the same workflow runs a hundred times, when validation is the only thing standing between you and a bad merge, and when you need the thousandth run to look like the first.

---

## Built to be Observed, Customized, and Reused

This is a starting point, not a product. Nothing here is meant to survive contact with your codebase unchanged.

The tests it ships are not your tests. The prompts it ships describe a demo app, not your domain. The roster names the models that were good the week it was written. All of that is supposed to be replaced, and the whole thing is shaped so that replacing it is a small edit in an obvious file instead of a rewrite. That is what those three properties are for. **Observable** so you can see which part is actually costing you. **Customizable** so the fix is one file. **Reusable** so you do it once and stamp it everywhere.

Where to start, roughly in the order that pays off fastest:

| Change | File | Why |
|---|---|---|
| Your real commands | `adws/adw_modules/quality.py` | The shipped blocks are placeholders that exit 0. Until you wire this, your test phase is theater |
| Your prompts | `adws/adw_data/prompt_engineering/{agent}/` | Where your standards live: what a good plan looks like, what a review has to catch |
| Your roster | `adws/adw_sssf_config/sssf.config.yaml` | Models, thinking levels, tools, and what each agent is allowed to write |
| Your chains | `adws/adw_*.py` | Copy the closest workflow and edit the phase list. They are 40 to 180 lines on purpose |
| Your definition of done | `adws/adw_modules/gates.py` | A gate is one function. Whatever "done" means where you work, write it here |
| Your agent capabilities | `adws/adw_data/harness_engineering/` | Pi extensions, a different set per agent if that is what the job needs |

And what it deliberately does not do. It runs on your current branch. There is no sandbox, no branch per run, no merge step, no cloud, and no human-in-the-loop approval phase. Those are the obvious next things to build. They are left out so the core stays small enough to read in one sitting, which is the only reason you would trust it enough to change it.

So take it. Fork it, strip the parts you do not need, rename the agents, throw out half the workflows, and roll what is left into the factory your product actually needs. The specific chains in here matter far less than the shape: code owns the loop, agents own the phases, and every run leaves a trace you can go read.

---

## See it in a real repo

The [`example` branch](../../tree/example) is this same skill with the factory already stamped in: a populated `adws/`, a `justfile`, a demo app the factory planned, built, tested, reviewed, and documented, and the specs, docs, and traces those runs produced.

```bash
git clone <this-repo> sssf && cd sssf
git checkout example
```

---

## License

MIT, see [`LICENSE`](LICENSE).

---

## Master Agentic Coding

<p align="center">
  <img src="images/08_rise_with_the_ceiling.svg" alt="Vibe coding sits inside a narrow band with a short arrow of headroom above it, agentic engineering rises far above that band with a tall one" width="850">
</p>

Vibe coding is not knowing how your system works, and not looking. Agentic engineering is knowing how your system works so well that you do not have to look.

Master agentic coding by gaining a deeper understanding of the foundational units of the software factory.

Learn tactical agentic coding patterns with [Tactical Agentic Coding](https://agenticengineer.com/tactical-agentic-coding?y=sssf).

Follow the [IndyDevDan YouTube channel](https://www.youtube.com/@indydevdan) to improve your agentic coding advantage.

---

Stay Focused and Keep Building

- IndyDevDan
