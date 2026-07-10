from __future__ import annotations

import os
import shutil
from pathlib import Path

from .util import LLVMManagerError

_WINDOWS_EXECUTABLE_SUFFIXES = {".exe", ".cmd", ".bat", ".com"}


def _is_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return path.suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES
    return os.access(path, os.X_OK)


def _alias_name(name: str, major: int) -> str:
    path = Path(name)
    if path.suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES:
        return f"{path.stem}-{major}{path.suffix}"
    return f"{name}-{major}"


def ensure_versioned_binaries(install_prefix: Path, major: int) -> list[Path]:
    bin_dir = install_prefix / "bin"
    if not bin_dir.is_dir():
        raise LLVMManagerError(f"LLVM install has no bin directory: {bin_dir}")

    created: list[Path] = []
    originals = [path for path in bin_dir.iterdir() if _is_executable(path)]
    for original in originals:
        if original.name.endswith(f"-{major}") or original.stem.endswith(f"-{major}"):
            continue
        alias = bin_dir / _alias_name(original.name, major)
        if alias.exists() or alias.is_symlink():
            continue
        if os.name == "nt":
            try:
                os.link(original, alias)
            except OSError:
                shutil.copy2(original, alias)
        else:
            alias.symlink_to(original.name)
        created.append(alias)

    clang_aliases = [bin_dir / f"clang-{major}", bin_dir / f"clang-{major}.exe"]
    if not any(path.exists() for path in clang_aliases):
        raise LLVMManagerError(f"Failed to create clang-{major} in {bin_dir}")
    return created
