from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .versioning import LLVMVersion


@dataclass(frozen=True)
class InstallInfo:
    prefix: Path
    clang: Path
    version: LLVMVersion | None
    managed: bool
    active: bool = False
    tag: str | None = None

    @property
    def label(self) -> str:
        version = self.version.display if self.version else "unknown"
        source = "managed" if self.managed else "external"
        active = ", active" if self.active else ""
        return f"LLVM {version} ({source}{active}) - {self.prefix}"

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["prefix"] = str(self.prefix)
        data["clang"] = str(self.clang)
        data["version"] = self.version.display if self.version else None
        return data
