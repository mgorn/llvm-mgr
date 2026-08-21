from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .util import probe_process, shutil_which


@dataclass(frozen=True)
class VisualStudioInstallation:
    path: Path
    display_name: str
    version: str | None = None


def _deduplicate_path_entries(entries: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_entry in entries:
        entry = os.path.expandvars(raw_entry.strip().strip('"'))
        if not entry:
            continue
        normalized = os.path.normcase(os.path.normpath(entry))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(entry)
    return result


def _persistent_windows_path_entries() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows always provides winreg
        return []

    entries: list[str] = []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for hive, key_path in locations:
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except (FileNotFoundError, OSError):
            continue
        if isinstance(value, str):
            entries.extend(value.split(os.pathsep))
    return entries


def find_vswhere() -> Path | None:
    if os.name != "nt":
        return None
    found = shutil_which("vswhere.exe") or shutil_which("vswhere")
    if found:
        try:
            return Path(found).resolve()
        except OSError:
            return Path(found).absolute()

    for variable in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(variable)
        if not base:
            continue
        candidate = Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def visual_studio_installations() -> list[VisualStudioInstallation]:
    if os.name != "nt":
        return []
    vswhere = find_vswhere()
    if vswhere is None:
        return []
    completed = probe_process(
        [
            vswhere,
            "-all",
            "-products",
            "*",
            "-format",
            "json",
            "-utf8",
        ],
        timeout=20,
    )
    if completed is None or completed.returncode != 0:
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    results: list[VisualStudioInstallation] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        installation_path = item.get("installationPath")
        if not isinstance(installation_path, str) or not installation_path:
            continue
        display_name = item.get("displayName")
        installation_version = item.get("installationVersion")
        results.append(
            VisualStudioInstallation(
                path=Path(installation_path),
                display_name=display_name if isinstance(display_name, str) and display_name else "Visual Studio",
                version=installation_version if isinstance(installation_version, str) else None,
            )
        )
    return results


def visual_studio_llvm_prefixes() -> list[Path]:
    if os.name != "nt":
        return []

    prefixes: list[Path] = []
    for installation in visual_studio_installations():
        llvm_root = installation.path / "VC" / "Tools" / "Llvm"
        candidates = [llvm_root]
        try:
            candidates.extend(
                child for child in sorted(llvm_root.iterdir(), key=lambda path: path.name.lower()) if child.is_dir()
            )
        except OSError:
            pass
        for candidate in candidates:
            try:
                if (candidate / "bin").is_dir():
                    prefixes.append(candidate)
            except OSError:
                continue
    return _deduplicate_paths(prefixes)


def windows_llvm_prefixes() -> list[Path]:
    if os.name != "nt":
        return []

    prefixes: list[Path] = []
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            prefixes.append(Path(base) / "LLVM")
    prefixes.extend(visual_studio_llvm_prefixes())

    existing: list[Path] = []
    for prefix in _deduplicate_paths(prefixes):
        try:
            if (prefix / "bin").is_dir():
                existing.append(prefix)
        except OSError:
            continue
    return existing


def _deduplicate_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            normalized = path.expanduser().resolve()
        except OSError:
            normalized = path.expanduser().absolute()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def augment_windows_search_path(search_path: str | None = None) -> str:
    base = search_path if search_path is not None else os.environ.get("PATH", "")
    if os.name != "nt" or search_path is not None:
        return base

    entries = [entry for entry in base.split(os.pathsep) if entry]
    entries.extend(_persistent_windows_path_entries())
    entries.extend(str(prefix / "bin") for prefix in windows_llvm_prefixes())
    return os.pathsep.join(_deduplicate_path_entries(entries))
