from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import ManagerPaths
from .models import InstallInfo
from .util import read_json, write_json
from .versioning import LLVMVersion, parse_llvm_tag, parse_version_text

_CLANG_NAMES = ("clang", "clang.exe")
_VERSIONED_CLANG_RE = re.compile(r"^clang-(\d+)(?:\.exe)?$")


def _candidate_clang(prefix: Path) -> Path | None:
    bin_dir = prefix / "bin"
    if not bin_dir.is_dir():
        return None
    for name in _CLANG_NAMES:
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    versioned = sorted(
        (item for item in bin_dir.iterdir() if item.is_file() and _VERSIONED_CLANG_RE.match(item.name)),
        reverse=True,
    )
    return versioned[0] if versioned else None


def _clang_version(clang: Path) -> LLVMVersion | None:
    try:
        result = subprocess.run(
            [str(clang), "--version"],
            text=True,
            capture_output=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version_text(result.stdout + "\n" + result.stderr)


def _metadata(prefix: Path) -> dict[str, object]:
    path = prefix / ".llvm-manager.json"
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def _active_prefix(paths: ManagerPaths) -> Path | None:
    if paths.current.is_symlink():
        try:
            return paths.current.resolve(strict=True)
        except OSError:
            return None
    state = read_json(paths.state_file, {})
    if isinstance(state, dict) and isinstance(state.get("active_prefix"), str):
        return Path(state["active_prefix"]).expanduser().resolve()
    return None


def _common_prefixes() -> list[Path]:
    prefixes: list[Path] = []
    clang_on_path = shutil.which("clang")
    if clang_on_path:
        prefixes.append(Path(clang_on_path).resolve().parent.parent)

    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                prefixes.extend([Path(base) / "LLVM", Path(base) / "LLVM" / "bin" / ".."])
    else:
        prefixes.extend(
            [
                Path("/usr"),
                Path("/usr/local"),
                Path("/opt/homebrew/opt/llvm"),
                Path("/opt/local/libexec/llvm-18"),
            ]
        )
        for parent in (Path("/usr/lib"), Path("/usr/local/opt"), Path("/opt/homebrew/opt")):
            if parent.is_dir():
                prefixes.extend(path for path in parent.glob("llvm*") if path.is_dir())
    return prefixes


def scan_installs(paths: ManagerPaths, *, include_external: bool = True) -> list[InstallInfo]:
    paths.ensure()
    active = _active_prefix(paths)
    candidates: list[tuple[Path, bool]] = []

    for child in paths.install_root.iterdir():
        if child.is_dir() and child.name != "current":
            candidates.append((child, True))

    if include_external:
        candidates.extend((prefix, False) for prefix in _common_prefixes())
        for path_entry in os.environ.get("PATH", "").split(os.pathsep):
            if not path_entry:
                continue
            bin_dir = Path(path_entry).expanduser()
            if bin_dir.name.lower() == "bin":
                candidates.append((bin_dir.parent, False))

    installs: list[InstallInfo] = []
    seen: set[Path] = set()
    for prefix, managed in candidates:
        try:
            resolved = prefix.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        clang = _candidate_clang(resolved)
        if not clang:
            continue
        seen.add(resolved)
        metadata = _metadata(resolved)
        tag = metadata.get("tag") if isinstance(metadata.get("tag"), str) else None
        version = parse_llvm_tag(tag) if tag else None
        if version is None:
            version = _clang_version(clang)
        installs.append(
            InstallInfo(
                prefix=resolved,
                clang=clang,
                version=version,
                managed=managed,
                active=active == resolved,
                tag=tag,
            )
        )

    installs.sort(
        key=lambda item: (
            item.active,
            item.version is not None,
            item.version or LLVMVersion(0, 0, 0),
            item.managed,
        ),
        reverse=True,
    )
    write_json(paths.scan_cache, [item.to_json() for item in installs])
    return installs


def print_installs(installs: list[InstallInfo]) -> None:
    if not installs:
        print("No LLVM/Clang installations found.")
        return
    for index, install in enumerate(installs, start=1):
        marker = "*" if install.active else " "
        print(f"{marker} {index:>2}) {install.label}")
