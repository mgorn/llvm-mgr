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
    cxx_standard_library: str | None = None
    cxx_standard_library_version: str | None = None
    managed_libcxx_available: bool = False

    @property
    def label(self) -> str:
        version = self.version.display if self.version else "unknown"
        source = "managed" if self.managed else "external"
        details = [source]
        if self.active:
            details.append("active")
        if self.cxx_standard_library == "managed-libc++":
            pairing = "paired libc++"
            if self.cxx_standard_library_version:
                pairing += f" {self.cxx_standard_library_version}"
            details.append(pairing)
        return f"LLVM {version} ({', '.join(details)}) - {self.prefix}"

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["prefix"] = str(self.prefix)
        data["clang"] = str(self.clang)
        data["version"] = self.version.display if self.version else None
        return data
