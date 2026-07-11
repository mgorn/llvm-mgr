from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .util import LLVMManagerError, probe_process, require_tools, run
from .versioning import LLVMVersion, normalize_tag, parse_llvm_tag

DEFAULT_REPOSITORY_URL = "https://github.com/llvm/llvm-project.git"
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class RevisionKind(str, Enum):
    TAG = "tag"
    BRANCH = "branch"
    COMMIT = "commit"


@dataclass(frozen=True)
class SourceRevision:
    kind: RevisionKind
    value: str

    def __post_init__(self) -> None:
        value = self.value.strip()
        if self.kind is RevisionKind.TAG:
            value = normalize_tag(value)
        if not value:
            raise LLVMManagerError(f"LLVM {self.kind.value} cannot be empty")
        if self.kind is RevisionKind.TAG and parse_llvm_tag(value) is None:
            raise LLVMManagerError(f"Not a recognized LLVM release tag: {value}")
        if self.kind is RevisionKind.COMMIT and not _COMMIT_RE.fullmatch(value):
            raise LLVMManagerError("Commit must be a 7- to 40-character hexadecimal Git commit ID")
        object.__setattr__(self, "value", value)

    @property
    def label(self) -> str:
        return f"{self.kind.value} {self.value}"

    @property
    def directory_name(self) -> str:
        if self.kind is RevisionKind.TAG:
            return self.value
        safe_value = _SAFE_NAME_RE.sub("-", self.value).strip("-._") or self.kind.value
        if self.kind is RevisionKind.COMMIT:
            safe_value = safe_value[:12]
        return f"{self.kind.value}-{safe_value}"


@dataclass(frozen=True)
class ResolvedRevision:
    requested: SourceRevision
    commit: str

    def to_json(self) -> dict[str, str]:
        return {
            "kind": self.requested.kind.value,
            "value": self.requested.value,
            "commit": self.commit,
        }


def fetch_release_tags(repository_url: str = DEFAULT_REPOSITORY_URL) -> list[LLVMVersion]:
    git = require_tools(("git",))["git"]
    result = run([git, "ls-remote", "--tags", "--refs", repository_url, "llvmorg-*"], capture=True)
    versions: set[LLVMVersion] = set()
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        version = parse_llvm_tag(fields[1].removeprefix("refs/tags/"))
        if version:
            versions.add(version)
    if not versions:
        raise LLVMManagerError(f"No LLVM release tags were found at {repository_url}")
    return sorted(versions, reverse=True)


def fetch_remote_branches(repository_url: str = DEFAULT_REPOSITORY_URL) -> list[str]:
    git = require_tools(("git",))["git"]
    result = run([git, "ls-remote", "--heads", repository_url], capture=True)
    branches = sorted(
        fields[1].removeprefix("refs/heads/")
        for line in result.stdout.splitlines()
        if len(fields := line.split(maxsplit=1)) == 2
    )
    if not branches:
        raise LLVMManagerError(f"No branches were found at {repository_url}")
    return branches


class LLVMRepository:
    def __init__(
        self,
        path: Path,
        repository_url: str = DEFAULT_REPOSITORY_URL,
        *,
        git: str | None = None,
    ) -> None:
        self.path = path
        self.repository_url = repository_url
        self.git = git or require_tools(("git",))["git"]

    def ensure_clone(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            run([self.git, "clone", self.repository_url, self.path])
            return
        if not (self.path / ".git").is_dir():
            raise LLVMManagerError(f"Existing source path is not a Git repository: {self.path}")

        result = run([self.git, "remote", "get-url", "origin"], cwd=self.path, capture=True)
        actual = result.stdout.strip()
        expected_path = Path(self.repository_url).expanduser()
        actual_path = Path(actual).expanduser()
        same_local_path = (
            expected_path.exists()
            and actual_path.exists()
            and expected_path.resolve() == actual_path.resolve()
        )
        if actual != self.repository_url and not same_local_path:
            raise LLVMManagerError(
                f"Repository origin mismatch at {self.path}: expected {self.repository_url!r}, found {actual!r}"
            )

    def fetch(self) -> None:
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
                f"LLVM source checkout contains local changes. Commit, stash, or remove them before switching revisions: {self.path}"
            )

    def _resolve(self, revision: SourceRevision) -> str:
        if revision.kind is RevisionKind.TAG:
            reference = f"refs/tags/{revision.value}^{{commit}}"
            unavailable = f"Tag is not available in the checkout: {revision.value}"
        elif revision.kind is RevisionKind.BRANCH:
            reference = f"refs/remotes/origin/{revision.value}^{{commit}}"
            unavailable = f"Branch is not available at origin: {revision.value}"
        else:
            reference = f"{revision.value}^{{commit}}"
            unavailable = f"Commit is not available in the checkout: {revision.value}"

        verify = probe_process([self.git, "rev-parse", "--verify", reference], cwd=self.path)
        if verify is None or verify.returncode != 0:
            raise LLVMManagerError(unavailable)
        return verify.stdout.strip()

    def checkout(self, revision: SourceRevision | str) -> ResolvedRevision:
        if isinstance(revision, str):
            revision = SourceRevision(RevisionKind.TAG, revision)
        self.fetch()
        self.ensure_clean()
        commit = self._resolve(revision)
        run([self.git, "checkout", "--detach", commit], cwd=self.path)
        return ResolvedRevision(revision, commit)
