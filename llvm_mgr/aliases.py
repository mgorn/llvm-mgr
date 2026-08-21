from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .util import LLVMManagerError

_WINDOWS_EXECUTABLE_SUFFIXES = {".exe", ".cmd", ".bat", ".com"}
_ALREADY_VERSIONED_RE = re.compile(r"-\d+(?:\.\d+)*(?:\.exe|\.cmd|\.bat|\.com)?$", re.IGNORECASE)
_LLVM_TOOL_RE = re.compile(
    r"(?:clang(?:\+\+|-cpp|-cl|-format|-tidy|-scan-deps)?|llvm-[A-Za-z0-9_.+-]+|"
    r"lld(?:-link)?|ld\.lld|wasm-ld)(?:\.exe|\.cmd|\.bat|\.com)?$",
    re.IGNORECASE,
)


def _is_executable(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        if os.name == "nt":
            return path.suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES
        return os.access(path, os.X_OK)
    except OSError:
        return False


def _alias_name(name: str, major: int) -> str:
    path = Path(name)
    if path.suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES:
        return f"{path.stem}-{major}{path.suffix}"
    return f"{name}-{major}"


def _alias_matches(alias: Path, original: Path) -> bool:
    try:
        if alias.is_symlink():
            target = os.readlink(alias)
            return (alias.parent / target).resolve() == original.resolve()
        return alias.exists() and os.path.samefile(alias, original)
    except OSError:
        return False


def _create_alias(original: Path, alias: Path) -> None:
    if os.name == "nt":
        try:
            os.link(original, alias)
        except OSError:
            shutil.copy2(original, alias)
    else:
        alias.symlink_to(original.name)


def ensure_versioned_binaries(install_prefix: Path, major: int) -> list[Path]:
    bin_dir = install_prefix / "bin"
    if not bin_dir.is_dir():
        raise LLVMManagerError(f"LLVM install has no bin directory: {bin_dir}")

    created: list[Path] = []
    originals = [
        path
        for path in bin_dir.iterdir()
        if _is_executable(path)
        and _LLVM_TOOL_RE.fullmatch(path.name)
        and not _ALREADY_VERSIONED_RE.search(path.name)
    ]
    for original in originals:
        alias = bin_dir / _alias_name(original.name, major)
        if alias.exists() or alias.is_symlink():
            if _alias_matches(alias, original):
                continue
            if alias.is_dir() and not alias.is_symlink():
                raise LLVMManagerError(f"Cannot replace version alias directory: {alias}")
            alias.unlink()
        _create_alias(original, alias)
        if not _alias_matches(alias, original):
            raise LLVMManagerError(f"Failed to create a valid alias from {alias} to {original}")
        created.append(alias)

    clang_aliases = [bin_dir / f"clang-{major}", bin_dir / f"clang-{major}.exe"]
    if not any(path.exists() and _is_executable(path) for path in clang_aliases):
        raise LLVMManagerError(f"Failed to create clang-{major} in {bin_dir}")
    return created


def ensure_windows_mt_alias(install_prefix: Path, *, windows: bool | None = None) -> list[Path]:
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return []

    bin_dir = install_prefix / "bin"
    llvm_mt = bin_dir / "llvm-mt.exe"
    if not llvm_mt.is_file():
        raise LLVMManagerError(f"Installed LLVM manifest tool was not found: {llvm_mt}")

    mt = bin_dir / "mt.exe"
    if mt.exists() or mt.is_symlink():
        if _alias_matches(mt, llvm_mt):
            return []
        if mt.is_dir() and not mt.is_symlink():
            raise LLVMManagerError(f"Cannot replace manifest-tool alias directory: {mt}")
        mt.unlink()

    _create_alias(llvm_mt, mt)
    if not _alias_matches(mt, llvm_mt):
        raise LLVMManagerError(f"Failed to create a valid alias from {mt} to {llvm_mt}")
    return [mt]
