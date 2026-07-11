from __future__ import annotations

import os
import re
from pathlib import Path

from .config import ManagerPaths
from .models import InstallInfo
from .util import probe_text, read_json, shutil_which
from .versioning import LLVMVersion, parse_llvm_tag, parse_version_text

_CLANG_NAMES = ("clang", "clang.exe")
_VERSIONED_CLANG_RE = re.compile(r"^clang-(\d+)(?:\.exe)?$", re.IGNORECASE)


def _is_executable(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        return os.name == "nt" or os.access(path, os.X_OK)
    except OSError:
        return False


def _candidate_clang(prefix: Path) -> Path | None:
    bin_dir = prefix / "bin"
    if not bin_dir.is_dir():
        return None
    for name in _CLANG_NAMES:
        candidate = bin_dir / name
        if _is_executable(candidate):
            return candidate

    versioned: list[tuple[int, str, Path]] = []
    try:
        entries = bin_dir.iterdir()
    except OSError:
        return None
    for item in entries:
        match = _VERSIONED_CLANG_RE.fullmatch(item.name)
        if match and _is_executable(item):
            versioned.append((int(match.group(1)), item.name.lower(), item))
    return max(versioned, default=(0, "", None))[2]


def _clang_version(clang: Path) -> LLVMVersion | None:
    output = probe_text([clang, "--version"], timeout=8)
    return parse_version_text(output) if output else None


def _metadata(prefix: Path) -> dict[str, object]:
    value = read_json(prefix / ".llvm-manager.json", {}, warn=True)
    return value if isinstance(value, dict) else {}


def _metadata_tag(metadata: dict[str, object]) -> str | None:
    # Read the current source schema, retaining compatibility with installs made
    # by llvm-manager 1.5 and earlier.
    source = metadata.get("source")
    if isinstance(source, dict) and source.get("kind") == "tag" and isinstance(source.get("value"), str):
        return source["value"]
    legacy = metadata.get("tag")
    return legacy if isinstance(legacy, str) else None


def _active_prefix(paths: ManagerPaths) -> Path | None:
    if paths.current.is_symlink():
        try:
            return paths.current.resolve(strict=True)
        except OSError:
            return None
    state = read_json(paths.state_file, {}, warn=True)
    if isinstance(state, dict) and isinstance(state.get("active_prefix"), str):
        try:
            return Path(state["active_prefix"]).expanduser().resolve()
        except OSError:
            return None
    return None


def inspect_install(
    prefix: Path,
    *,
    managed: bool | None = None,
    active_prefix: Path | None = None,
) -> InstallInfo | None:
    try:
        resolved = prefix.expanduser().resolve()
    except OSError:
        return None
    if resolved.name.lower() == "bin":
        resolved = resolved.parent

    clang = _candidate_clang(resolved)
    if clang is None:
        return None
    probed_version = _clang_version(clang)
    if probed_version is None:
        return None

    metadata = _metadata(resolved)
    tag = _metadata_tag(metadata)
    tag_version = parse_llvm_tag(tag) if tag else None
    manager_owned = (resolved / ".llvm-manager.json").is_file() if managed is None else managed
    return InstallInfo(
        prefix=resolved,
        clang=clang,
        version=tag_version or probed_version,
        managed=manager_owned,
        active=active_prefix == resolved,
        tag=tag,
    )


def _common_prefixes(search_path: str | None = None) -> list[Path]:
    prefixes: list[Path] = []
    clang_on_path = shutil_which("clang", search_path)
    if clang_on_path:
        prefixes.append(Path(clang_on_path).resolve().parent.parent)

    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                prefixes.append(Path(base) / "LLVM")
    else:
        prefixes.extend((Path("/usr"), Path("/usr/local")))
        glob_roots = (
            Path("/usr/lib"),
            Path("/usr/local/opt"),
            Path("/opt/homebrew/opt"),
            Path("/opt/local/libexec"),
        )
        for parent in glob_roots:
            if parent.is_dir():
                try:
                    prefixes.extend(path for path in parent.glob("llvm*") if path.is_dir())
                except OSError:
                    continue

    unique: list[Path] = []
    seen: set[Path] = set()
    for prefix in prefixes:
        try:
            normalized = prefix.expanduser().resolve()
        except OSError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def scan_installs(
    paths: ManagerPaths,
    *,
    include_external: bool = True,
    search_path: str | None = None,
) -> list[InstallInfo]:
    active = _active_prefix(paths)
    candidates: list[tuple[Path, bool]] = []

    if paths.install_root.is_dir():
        try:
            candidates.extend(
                (child, True)
                for child in paths.install_root.iterdir()
                if child.is_dir() and child.name != "current"
            )
        except OSError:
            pass

    path_value = search_path if search_path is not None else os.environ.get("PATH", "")
    if include_external:
        candidates.extend((prefix, False) for prefix in _common_prefixes(path_value))
        for path_entry in path_value.split(os.pathsep):
            if not path_entry:
                continue
            bin_dir = Path(path_entry).expanduser()
            if bin_dir.name.lower() == "bin":
                candidates.append((bin_dir.parent, False))

    installs: list[InstallInfo] = []
    seen: set[Path] = set()
    for prefix, managed in candidates:
        install = inspect_install(prefix, managed=managed, active_prefix=active)
        if install is None or install.prefix in seen:
            continue
        seen.add(install.prefix)
        installs.append(install)

    installs.sort(
        key=lambda item: (
            item.active,
            item.version is not None,
            item.version or LLVMVersion(0, 0, 0),
            item.managed,
        ),
        reverse=True,
    )
    return installs
