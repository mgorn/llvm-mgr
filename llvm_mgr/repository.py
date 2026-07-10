from __future__ import annotations

import subprocess
from pathlib import Path

from .util import LLVMManagerError, require_tools, run
from .versioning import LLVMVersion, parse_llvm_tag

DEFAULT_REPOSITORY_URL = "https://github.com/llvm/llvm-project.git"


def fetch_release_tags(repository_url: str = DEFAULT_REPOSITORY_URL) -> list[LLVMVersion]:
    git = require_tools(["git"])["git"]
    result = run(
        [git, "ls-remote", "--tags", "--refs", repository_url, "llvmorg-*"],
        capture=True,
    )
    versions: set[LLVMVersion] = set()
    for line in result.stdout.splitlines():
        try:
            reference = line.split(maxsplit=1)[1]
        except IndexError:
            continue
        tag = reference.removeprefix("refs/tags/")
        version = parse_llvm_tag(tag)
        if version:
            versions.add(version)
    if not versions:
        raise LLVMManagerError(f"No LLVM release tags were found at {repository_url}")
    return sorted(versions, reverse=True)


class LLVMRepository:
    def __init__(self, path: Path, repository_url: str = DEFAULT_REPOSITORY_URL) -> None:
        self.path = path
        self.repository_url = repository_url
        self.git = require_tools(["git"])["git"]

    def ensure_clone(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            run([self.git, "clone", self.repository_url, self.path])
            return

        if not (self.path / ".git").is_dir():
            raise LLVMManagerError(f"Existing source path is not a Git repository: {self.path}")

        result = run(
            [self.git, "remote", "get-url", "origin"],
            cwd=self.path,
            capture=True,
        )
        actual = result.stdout.strip()
        expected_path = Path(self.repository_url).expanduser()
        actual_path = Path(actual).expanduser()
        same_local_path = expected_path.exists() and actual_path.exists() and expected_path.resolve() == actual_path.resolve()
        if actual != self.repository_url and not same_local_path:
            raise LLVMManagerError(
                f"Repository origin mismatch at {self.path}: expected {self.repository_url!r}, found {actual!r}"
            )

    def fetch_tags(self) -> None:
        self.ensure_clone()
        run([self.git, "fetch", "origin", "--tags", "--force", "--prune"], cwd=self.path)

    def ensure_clean(self) -> None:
        result = run(
            [self.git, "status", "--porcelain", "--untracked-files=normal"],
            cwd=self.path,
            capture=True,
        )
        if result.stdout.strip():
            raise LLVMManagerError(
                f"LLVM source checkout contains local changes. Commit, stash, or remove them before switching tags: {self.path}"
            )

    def checkout(self, tag: str) -> None:
        self.fetch_tags()
        self.ensure_clean()
        verify = subprocess.run(
            [self.git, "rev-parse", "--verify", f"refs/tags/{tag}"],
            cwd=self.path,
            text=True,
            capture_output=True,
        )
        if verify.returncode != 0:
            raise LLVMManagerError(f"Tag is not available in the checkout: {tag}")
        run([self.git, "checkout", "--detach", tag], cwd=self.path)
        run([self.git, "reset", "--hard", tag], cwd=self.path)
