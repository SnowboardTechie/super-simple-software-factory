"""Low-level git operations for code phases. All low-level logic lives in adw_modules.

Every call names the repository it means. v1 ran `git` with no `cwd`, so each
command landed wherever the process happened to be — which is the same thing as
saying a run can only ever work on one repository, the one it was launched
from. `cwd=None` still means "the process's own directory", so nothing that
passed no argument behaves differently.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Optional

Cwd = Optional[Path | str]


def _git_raw(*args: str, cwd: Cwd = None) -> str:
    """Exactly what git printed. `--porcelain -z` leads with a status column
    whose first character is often a space, and stripping it eats the path."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _git(*args: str, cwd: Cwd = None) -> str:
    return _git_raw(*args, cwd=cwd).strip()


def current_branch(cwd: Cwd = None) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


def create_branch(name: str, cwd: Cwd = None) -> str:
    _git("checkout", "-b", name, cwd=cwd)
    return name


def is_repo(cwd: Cwd = None) -> bool:
    result = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=cwd,
                            capture_output=True, text=True)
    return result.returncode == 0


def common_dir(cwd: Cwd = None) -> Path:
    """The repository's shared git directory — the identity a worktree belongs to.

    Every linked worktree of a repository reports the SAME common dir while
    reporting its own `--git-dir`, which is what makes "is this worktree one of
    yours" a single comparison. Git prints it relative to `cwd` at the top of a
    normal repo, so it is anchored here rather than trusted as absolute.
    """
    out = _git("rev-parse", "--git-common-dir", cwd=cwd)
    path = Path(out)
    return (path if path.is_absolute() else Path(cwd or Path.cwd()) / path).resolve()


def repo_root(cwd: Cwd = None) -> Path:
    """Absolute root of the codebase — where agents are spawned to work.

    The git toplevel when there is one, else `cwd` (ADWs run fine in a non-git
    dir; only a commit phase requires a repo). Always absolute, so it is safe to
    hand to a subprocess regardless of where the ADW was launched from.
    """
    if is_repo(cwd=cwd):
        return Path(_git("rev-parse", "--show-toplevel", cwd=cwd)).resolve()
    return Path(cwd or Path.cwd()).resolve()


# ── the working tree, as a set of paths ──────────────────────────────────────

def dirty_paths(cwd: Cwd = None, include_ignored: bool = False) -> list[str]:
    """Every path the working tree differs on, relative to the repo root.

    One answer for tracked edits, deletions, renames (both sides), and
    untracked files, so nothing downstream has to parse status letters twice.
    `include_ignored` adds gitignored entries: an individually-reported ignored
    FILE (`.env`) comes back by name; a wholly-ignored DIRECTORY comes back as
    the directory entry, because git does not walk inside one — see
    permissions.snapshot for what that means for detection.
    """
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored")
    fields = _git_raw(*args, cwd=cwd).split("\0")
    paths: set[str] = set()
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.add(path)
        # A rename/copy entry is followed by its SOURCE path in the next field.
        # Both sides changed, so both are reported.
        if "R" in status or "C" in status:
            if i < len(fields) and fields[i]:
                paths.add(fields[i])
            i += 1
    return sorted(paths)


def hidden_index_entries(cwd: Cwd = None) -> dict[str, str]:
    """Tracked paths whose index flags suppress status, mapped to their tag.

    `git ls-files -v` lowercases the ordinary tag when assume-unchanged is set;
    skip-worktree uses `S` (or `s` when both flags apply). These paths must be
    fingerprinted even while porcelain says the tree is clean.
    """
    fields = _git_raw("ls-files", "-v", "-z", cwd=cwd).split("\0")
    return {entry[2:]: entry[0] for entry in fields
            if len(entry) >= 3 and entry[0] in {"h", "s", "S"}}


def hidden_index_paths(cwd: Cwd = None) -> list[str]:
    return sorted(hidden_index_entries(cwd=cwd))


def index_fingerprints(paths: Iterable[str], cwd: Cwd = None) -> dict[str, str]:
    """The stage-zero index identity for paths, shaped like `blob_ids()` output."""
    wanted = sorted(set(paths))
    if not wanted:
        return {}
    out: dict[str, str] = {}
    fields = _git_raw("ls-files", "--stage", "-z", "--", *wanted,
                      cwd=cwd).split("\0")
    for entry in fields:
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, sha, stage = metadata.split(" ")
        if stage != "0":
            continue
        prefix = "x" if mode == "100755" else "l" if mode == "120000" else "f"
        out[path] = f"{prefix}:{sha}"
    return out


def blob_ids(paths: Iterable[str], cwd: Cwd = None) -> dict[str, str]:
    """A `<type>:<content>` fingerprint per path — what a change is measured by.

    Content alone is not identity. `chmod +x` rewrites no byte, so a fingerprint
    made of content only reported nothing at all when an agent made a script
    executable — and on an ALREADY-DIRTY file, where the path is in the diff
    either way, the mode was the only thing left that could have told you. The
    type/mode component carries it:

        f:<sha>   a regular file            x:<sha>   an executable file
        l:<sha>   a symlink, hashed by its TARGET PATH, not the target's bytes
        directory  a wholly-ignored tree git does not walk into
        absent     deleted, or a rename's source side

    A symlink is hashed from `readlink` because git does the same, and because
    following it would fingerprint a file that is not the one being watched —
    and would raise on a broken link.
    """
    root = Path(repo_root(cwd=cwd))
    wanted = list(paths)
    files, out = [], {}
    for path in wanted:
        target = root / path
        if target.is_symlink():
            out[path] = f"l:{hash_text(str(os.readlink(target)))}"
        elif target.is_dir():
            out[path] = "directory"
        elif target.exists():
            files.append(path)
        else:
            out[path] = "absent"
    if files:
        # One call for the whole set: hash-object takes many paths and answers
        # in order, and a dirty set is small by construction.
        hashes = _git("hash-object", "--", *files, cwd=cwd).splitlines()
        for path, blob in zip(files, hashes):
            executable = bool((root / path).stat().st_mode & 0o111)
            out[path] = f"{'x' if executable else 'f'}:{blob}"
    return out


def hash_text(text: str) -> str:
    """git's blob id for a string, computed locally — no subprocess for a link."""
    body = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()


# ── committing ───────────────────────────────────────────────────────────────

def commit_paths(message: str, paths: Iterable[str], cwd: Cwd = None) -> str:
    """Stage EXACTLY `paths` and commit them. Returns the new short sha.

    This replaces `commit_all()`, which ran `git add -A`. That swept the whole
    working tree into the agent's commit — the engineer's half-finished edit in
    another file, their scratch notes, an unrelated branch's work — and signed
    it with the agent's message. There is deliberately no compatibility path
    back to it: a commit phase now says which paths it means, and is refused if
    the repository disagrees.

    `paths` is the ACCEPTED CHANGE SET — what permissions.enforce watched the
    agents actually change and allowed (`run.take_changes()`). Three ways this
    raises, all of them a phase failure:

      * nothing was accepted — a phase that changed nothing does not commit
      * a path is not actually dirty — the claim does not match the repository
      * the commit landed a different set than was asked for — a directory
        pathspec that widened, or a hook that added something
    """
    if not is_repo(cwd=cwd):
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the "
            "repo root (and make a first commit) before running an ADW that commits.")

    root = repo_root(cwd=cwd)
    wanted = sorted({p.strip() for p in paths if p and p.strip()})
    if not wanted:
        raise RuntimeError(
            "nothing to commit — no accepted change set reached the commit phase. "
            "The preceding phases changed no file the agents were permitted to change.")

    for path in wanted:
        candidate = (root / path).resolve() if not Path(path).is_absolute() \
            else Path(path).resolve()
        if candidate != root and root not in candidate.parents:
            raise RuntimeError(f"refusing to commit {path!r}: outside the repository ({root})")

    dirty = set(dirty_paths(cwd=cwd, include_ignored=False))
    unchanged = [p for p in wanted if p not in dirty]
    if unchanged:
        raise RuntimeError(
            "refusing to commit path(s) the repository shows no change to: "
            + ", ".join(unchanged))

    _git("add", "--", *wanted, cwd=cwd)          # `add` is what picks up new files
    _git("commit", "-m", message, "--", *wanted, cwd=cwd)

    # --no-renames so the check compares the same spelling it asked for: with
    # rename detection on, `old.py` + `new.py` come back as one entry.
    landed = set(_git("show", "--pretty=format:", "--name-only", "--no-renames",
                      "HEAD", cwd=cwd).split("\n")) - {""}
    if landed != set(wanted):
        raise RuntimeError(
            f"commit landed a different file set than was accepted — "
            f"asked for {sorted(wanted)}, committed {sorted(landed)}")
    return _git("rev-parse", "--short", "HEAD", cwd=cwd)


def changed_files(cwd: Cwd = None) -> list[str]:
    return dirty_paths(cwd=cwd)


# ── diff plumbing (composed into a ChangeSet by changes.py) ──────────────────

def ref_exists(ref: str, cwd: Cwd = None) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            cwd=cwd, capture_output=True, text=True)
    return result.returncode == 0


def rev(ref: str = "HEAD", cwd: Cwd = None) -> str:
    return _git("rev-parse", ref, cwd=cwd)


def short_sha(ref: str = "HEAD", cwd: Cwd = None) -> str:
    return _git("rev-parse", "--short", ref, cwd=cwd)


def symbolic_head(cwd: Cwd = None) -> str:
    """The full branch ref HEAD names, or an empty string when detached."""
    result = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=cwd,
                            capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return ""
    raise RuntimeError(f"git symbolic-ref -q HEAD failed: {result.stderr.strip()}")


def ref_map(cwd: Cwd = None) -> dict[str, str]:
    """Every reachable local ref and its object id, deterministically ordered."""
    refs: dict[str, str] = {}
    for line in _git_raw("for-each-ref", "--format=%(refname) %(objectname)",
                         cwd=cwd).splitlines():
        name, sha = line.split(" ", 1)
        refs[name] = sha
    return refs


def repository_state(cwd: Cwd = None) -> dict:
    """Git state outside the worktree that an agent phase must not mutate.

    Worktree status cannot see a clean self-commit, a branch/tag update, or an
    index-only change. Capturing HEAD, all refs, and the exact index bytes closes
    those holes while preserving a user's pre-existing staged state and flags.
    """
    raw_index = _git("rev-parse", "--git-path", "index", cwd=cwd)
    index_path = Path(raw_index)
    if not index_path.is_absolute():
        index_path = Path(cwd or Path.cwd()) / index_path
    index_path = index_path.resolve()
    index_bytes = index_path.read_bytes() if index_path.exists() else None
    index_mode = index_path.stat().st_mode & 0o777 if index_path.exists() else 0o644

    return {
        "head": rev(cwd=cwd),
        "head_ref": symbolic_head(cwd=cwd),
        "refs": ref_map(cwd=cwd),
        # Exact bytes, not `git write-tree`: index flags such as
        # assume-unchanged and skip-worktree do not affect the tree object, but
        # can make porcelain hide a later worktree mutation completely.
        "index_path": str(index_path),
        "index_bytes": index_bytes,
        "index_mode": index_mode,
    }


def restore_repository_state(state: dict, cwd: Cwd = None) -> None:
    """Restore refs, HEAD attachment, and index without overwriting worktree bytes.

    The caller separately reconciles worktree paths. Keeping that split is what
    preserves pre-existing dirty bytes: ref/index plumbing never checks a file
    out over the operator's edit.
    """
    before_refs = dict(state["refs"])
    current_refs = ref_map(cwd=cwd)

    # Recreate/reset the baseline refs first so HEAD always has a valid place to
    # attach. `update-ref` is plumbing: it does not rewrite the working tree.
    for name, sha in before_refs.items():
        _git("update-ref", name, sha, cwd=cwd)

    if state["head_ref"]:
        _git("symbolic-ref", "HEAD", state["head_ref"], cwd=cwd)
    else:
        _git("update-ref", "--no-deref", "HEAD", state["head"], cwd=cwd)

    # HEAD no longer points at an agent-created branch, so introduced refs can
    # now be removed safely. Objects become unreachable but are not force-pruned.
    for name in sorted(set(current_refs) - set(before_refs)):
        _git("update-ref", "-d", name, cwd=cwd)

    index_path = Path(state["index_path"])
    index_bytes = state["index_bytes"]
    if index_bytes is None:
        index_path.unlink(missing_ok=True)
        return

    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="index.sssf-restore-",
                                         dir=index_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(index_bytes)
            os.fchmod(handle.fileno(), state["index_mode"])
        os.replace(temporary, index_path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def merge_base(ref: str, other: str = "HEAD", cwd: Cwd = None) -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no ADW has to branch on it.
    """
    return _git("merge-base", ref, other, cwd=cwd)


def is_dirty(cwd: Cwd = None) -> bool:
    return bool(_git("status", "--porcelain", cwd=cwd))


def untracked_files(cwd: Cwd = None) -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard", cwd=cwd)
    return [line for line in out.splitlines() if line]


def diff_files(base: str, cwd: Cwd = None) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git("diff", "--name-only", base, cwd=cwd)
    return [line for line in out.splitlines() if line]


def diff_stat(base: str, cwd: Cwd = None) -> str:
    return _git("diff", "--stat", base, cwd=cwd)


def diff_counts(base: str, cwd: Cwd = None) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git("diff", "--numstat", base, cwd=cwd).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(base: str, cwd: Cwd = None) -> str:
    return _git("diff", base, cwd=cwd)
