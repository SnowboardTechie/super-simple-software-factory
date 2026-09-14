"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import git_helper
from .data_types import AgentConfig, SSSFConfig

# Bytecode every python import writes. In the TARGET WORKTREE it is a by-product
# of running the code under work, not a change to it, and without this exemption
# a read-only agent that so much as runs a script breaches its own permission.
# It does NOT extend to a protected root — see `_allowed`: a .pyc in the factory
# is executable Python in a tree no agent may write to.
BUILD_NOISE = ["**/__pycache__/", "**/*.pyc", "**/*.pyo"]


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


ROOT_SEP = "::"          # "<root label>::<path>" for anything outside the worktree


class TreeSnapshot(dict[str, str]):
    """Worktree fingerprints plus Git state that porcelain cannot represent."""

    def __init__(self):
        super().__init__()
        self.repo_states: dict[str, dict] = {}
        self.dirty_keys: set[str] = set()


def _key(label: str, path: str) -> str:
    return f"{label}{ROOT_SEP}{path}" if label else path


def split_key(key: str) -> tuple[str, str]:
    """('', 'src/app.py') for the target worktree; ('control', 'adws/…') otherwise."""
    label, _, path = key.rpartition(ROOT_SEP)
    return (label, path) if label else ("", key)


def _watched_roots(run) -> list[tuple[str, Path]]:
    """Every tree this run watches: the target worktree, plus protected roots."""
    return [("", Path(run.repo_root)),
            *((label, Path(root))
              for label, root in run.targets.protected_roots.items())]


def _exempt(rel: str) -> bool:
    """Interpreter build noise inside the target worktree."""
    return any(_matches(rel, pattern) for pattern in BUILD_NOISE)


def snapshot(run) -> dict[str, str]:
    """Fingerprint every path the working tree currently differs on, BY CONTENT.

    v1 fingerprinted a tracked file as its `git diff --numstat` counts —
    `"3,3"`. Two different rewrites of the same already-dirty file produce the
    same counts, so an agent could replace an engineer's uncommitted work
    byte-for-byte and the before/after comparison saw nothing at all. The
    fingerprint is now git's own object id for the file's current content, so
    any difference in bytes is a difference in fingerprint.

    Gitignored paths are included. `.env` is gitignored, and so is every secret
    file shaped like it; leaving them out made the one category of file most
    worth protecting the one category nothing watched. Two consequences worth
    stating plainly:

      * the session runtime now shows up, which is why the exact resolved
        `state_root` is removed by path identity rather than granting the same
        relative `data_dir` path in every watched repository;
      * git does not walk INTO a wholly-ignored directory — it reports
        `node_modules/` as one entry — so a change to a file inside one is not
        detected. The directory appearing or disappearing is. This is a real
        limit of a git-based fingerprint and is not a sandbox.

    EVERY watched root is fingerprinted, not only the target worktree. A key is
    the plain repo-relative path for the worktree and `<label>::<path>` for a
    protected root, so the two can never be confused for one another and a
    breach message says which tree it means. Paths under `state_root` are left
    out wherever they turn up: in the default layout the runtime lives INSIDE
    control_root, and every run would otherwise breach on its own handoff files.
    A state root that CONTAINED a watched root would silence it completely by
    the same rule, which is why `RunTargets.resolve` refuses that topology
    outright rather than leaving it to be discovered here.
    """
    state = Path(run.targets.state_root).resolve()
    fingerprints = TreeSnapshot()
    for label, root in _watched_roots(run):
        if not git_helper.is_repo(cwd=root):
            continue
        fingerprints.repo_states[label] = git_helper.repository_state(cwd=root)
        dirty = set(git_helper.dirty_paths(cwd=root, include_ignored=True))
        hidden_entries = git_helper.hidden_index_entries(cwd=root)
        hidden = set(hidden_entries)
        indexed = git_helper.index_fingerprints(hidden, cwd=root)
        paths = [p for p in sorted(dirty | hidden) if not _under(root / p, state)]
        for path, fingerprint in git_helper.blob_ids(paths, cwd=root).items():
            key = _key(label, path)
            fingerprints[key] = fingerprint
            hidden_dirty = (path in hidden and fingerprint != indexed.get(path)
                            # An absent skip-worktree path is the normal sparse
                            # checkout state, and can be restored by deletion.
                            and not (fingerprint == "absent"
                                     and hidden_entries[path].lower() == "s"))
            if path in dirty or hidden_dirty:
                fingerprints.dirty_keys.add(key)
    return fingerprints


def _under(path: Path, root: Path) -> bool:
    """True when `path` is `root` or sits inside it. Never raises on a missing path."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def repo_changes(run) -> list[str]:
    """What this phase changed in the target repo, excluding its own runtime.

    Measured from `run.tree_baseline` — the snapshot taken before the agent was
    handed the tree — so an engineer's pre-existing dirty file is not counted
    against the agent that never touched it. Without a baseline (a code phase
    asking the same question outside an agent call) it is the whole dirty set.
    """
    after = snapshot(run)
    baseline = getattr(run, "tree_baseline", None)
    touched = changed_paths(baseline, after) if baseline is not None else sorted(after)
    # Protected-root keys are excluded by construction: nothing outside the
    # target worktree is a change to the target repo, and nothing outside it may
    # ever reach a commit phase. Those paths are a BREACH, raised by enforce().
    return [p for p in touched
            if not split_key(p)[0] and not _exempt(p)]


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def changed_repo_states(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Labels of repositories whose HEAD, refs, or index changed."""
    old = getattr(before, "repo_states", {})
    new = getattr(after, "repo_states", {})
    return sorted({label for label in set(old) | set(new)
                   if old.get(label) != new.get(label)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories", and a
    leading `**/` means "at any depth INCLUDING the top", as gitignore spells
    it — so `**/*.pyc` covers `app.pyc` as well as `src/app.pyc`.
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if "*" in pattern or "?" in pattern:           # a wildcard directory still
        expanded = pattern + "**" if pattern.endswith("/") else pattern
        return _glob(expanded).fullmatch(path) is not None
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    return path == pattern


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """Build noise first, then the agent's own list, then what is protected.

    Runtime is not a relative allowlist entry: snapshot removes only the exact
    resolved state_root. That matters when control and target repositories are
    separate and both happen to contain `adws/adw_data/`.
    """
    if _exempt(path):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _roll_back(run, key: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.

    The key carries its own root, so a tampered factory file is restored in the
    factory's repository and a trunk edit in the trunk's — the undo happens
    where the write happened.
    """
    label, path = split_key(key)
    root = Path(run.targets.protected_roots[label] if label else run.repo_root)
    dirty_before = getattr(before, "dirty_keys", set(before))
    if key in dirty_before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if key not in after else "left as-is (was already modified)"
    if before.get(key) == "absent":
        try:
            target = root / path
            if target.is_dir() and not target.is_symlink():
                return "could not restore absent path (now a directory)"
            target.unlink(missing_ok=True)
            return "restored absent path"
        except OSError as error:
            return f"could not restore absent path ({error})"
    # Introduced by the agent. Whether git knows the path decides how to undo
    # it: a tracked file has committed bytes to restore, an untracked or
    # gitignored one has none and simply should not be there.
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path],
                             cwd=root, capture_output=True, text=True)
    if tracked.returncode != 0:
        try:
            (root / path).unlink()
            return "deleted"
        except OSError as error:
            return f"could not delete ({error})"
    result = subprocess.run(["git", "checkout", "--ignore-skip-worktree-bits",
                             "--", path],
                            cwd=root, capture_output=True, text=True)
    return "rolled back" if result.returncode == 0 else "could not roll back"


def _allowed(key: str, agent: AgentConfig, run) -> bool:
    """One verdict per detected path, whichever root it came from.

    A protected root is DENIED, full stop. No allowlist is consulted, because
    `writes:` describes what an agent may change in the codebase it was given
    and was never a licence to edit the factory judging it — and no BUILD_NOISE
    exemption either. Bytecode is noise in the target worktree, where it is a
    by-product of running the code under work; in the factory or the trunk a
    `.pyc` is executable Python appearing in a tree an agent may not write to,
    which is precisely what protected_files exists to stop.

    The ADW process does write bytecode into the control root when it imports
    adw_modules — but that happens at startup, before any phase opens, so it is
    already in the baseline and shows up as no change at all. A `.pyc` that
    appears DURING an agent phase was put there by the agent.
    """
    label, path = split_key(key)
    if not label:
        return permitted(path, agent, run.cfg)
    return False


def _root_for(run, label: str) -> Path:
    return Path(run.targets.protected_roots[label] if label else run.repo_root)


def _restore_git_state(run, labels: list[str], before: TreeSnapshot) -> dict[str, str]:
    """Undo Git metadata changes, then undo bytes they exposed in the worktree.

    A clean self-commit is invisible to porcelain. Restoring refs and the index
    exposes its bytes as ordinary worktree changes; paths that were clean before
    the phase can then be restored safely. A path already dirty at baseline is
    never reconstructed, for the same reason as an ordinary permission breach.
    """
    outcomes: dict[str, str] = {}
    for label in labels:
        key = _key(label, "<git-state>")
        try:
            git_helper.restore_repository_state(before.repo_states[label],
                                                cwd=_root_for(run, label))
            outcomes[key] = "refs, HEAD, and index restored"
        except Exception as error:
            outcomes[key] = f"could not restore Git state ({error})"

    restored = snapshot(run)
    for key in changed_paths(before, restored):
        outcomes[key] = _roll_back(run, key, before, restored)
    return outcomes


def enforce(run, phase, agent: AgentConfig, before: TreeSnapshot) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    after = snapshot(run)
    repo_state_breaches = changed_repo_states(before, after)
    if repo_state_breaches:
        outcomes = _restore_git_state(run, repo_state_breaches, before)
        detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
        raise PermissionBreach(
            f"{agent.name} modified Git history, refs, HEAD, or the index in "
            f"{len(repo_state_breaches)} repository root(s); agent phases may only "
            f"change permitted worktree paths:\n{detail}")

    touched = changed_paths(before, after)
    breaches = [k for k in touched if not _allowed(k, agent, run)]
    if not breaches:
        return touched

    outcomes = {k: _roll_back(run, k, before, after) for k in breaches}
    protected = sorted(run.targets.protected_roots)
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    if protected:
        scope += f" and from the {', '.join(protected)} root(s) entirely"
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")
