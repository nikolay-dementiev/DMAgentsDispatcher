"""Real temporary Git repositories for adapter tests, and a byte-level snapshot of a checkout."""

import os
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

GIT_ENVIRONMENT: Mapping[str, str] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "example@example.invalid",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "example@example.invalid",
}


def git(repo: str | Path, *args: str, check: bool = True) -> str:
    """Run a plain Git command in `repo` with a fixed identity and no host configuration."""
    result = subprocess.run(  # noqa: S603 - fixed argument array, test fixture
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from PATH is the point
        capture_output=True,
        text=True,
        check=check,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(repo), **GIT_ENVIRONMENT},
    )
    return result.stdout


def make_repo(directory: Path, files: Mapping[str, str] | None = None) -> str:
    """Create a repository with one commit holding `files`; return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q", "-b", "main")
    for name, content in (files or {"README.md": "hello\n"}).items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "initial")
    return str(directory)


def checkout_bytes(repo: str | Path, *, ignore: Iterable[str] = ()) -> dict[str, bytes]:
    """Map every file of the checkout to its bytes, except object storage and worktree admin.

    Reading raw bytes (not `git status`) is what proves a run left the checkout untouched.
    Paths containing any of the `ignore` fragments are left out.
    """
    root = Path(repo)
    skipped = (".git/objects", ".git/worktrees", *ignore)
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(fragment in relative for fragment in skipped) or path.is_dir():
            continue
        snapshot[relative] = (
            str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
        )
    return snapshot
