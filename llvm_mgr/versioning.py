from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_TAG_RE = re.compile(
    r"^llvmorg-(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?:-|)(?P<pre>rc\d+|git))?$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")


@total_ordering
@dataclass(frozen=True)
class LLVMVersion:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    @property
    def tag(self) -> str:
        suffix = f"-{self.prerelease}" if self.prerelease else ""
        return f"llvmorg-{self.major}.{self.minor}.{self.patch}{suffix}"

    @property
    def display(self) -> str:
        suffix = f"-{self.prerelease}" if self.prerelease else ""
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"

    @property
    def stable(self) -> bool:
        return self.prerelease is None

    def _key(self) -> tuple[int, int, int, int, int]:
        if self.prerelease is None:
            pre_rank = 2
            pre_number = 0
        elif self.prerelease.lower().startswith("rc"):
            pre_rank = 1
            pre_number = int(self.prerelease[2:])
        else:
            pre_rank = 0
            pre_number = 0
        return self.major, self.minor, self.patch, pre_rank, pre_number

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, LLVMVersion):
            return NotImplemented
        return self._key() < other._key()


def parse_llvm_tag(tag: str) -> LLVMVersion | None:
    match = _TAG_RE.fullmatch(tag.strip())
    if not match:
        return None
    return LLVMVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=match.group("pre"),
    )


def parse_version_text(text: str) -> LLVMVersion | None:
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return LLVMVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
    )


def normalize_tag(value: str) -> str:
    value = value.strip()
    if value.startswith("llvmorg-"):
        return value
    if re.fullmatch(r"\d+\.\d+\.\d+(?:-rc\d+)?", value):
        return f"llvmorg-{value}"
    return value


def latest_per_major(versions: list[LLVMVersion]) -> list[LLVMVersion]:
    latest: dict[int, LLVMVersion] = {}
    for version in versions:
        if not version.stable:
            continue
        current = latest.get(version.major)
        if current is None or version > current:
            latest[version.major] = version
    return sorted(latest.values(), reverse=True)
