from __future__ import annotations

import json
import os
import platform
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping

from .util import LLVMManagerError, extract_numeric_version, probe_process, probe_text, shutil_which

_SAFE_ID_RE = re.compile(r"[^a-z0-9]+")
_COMPILER_EXECUTABLE_RE = re.compile(
    r"(?:cc|c\+\+|cl|clang-cl|clang(?:\+\+)?(?:-\d+(?:\.\d+)*)?|gcc(?:-\d+(?:\.\d+)*)?|g\+\+(?:-\d+(?:\.\d+)*)?)"
)


@dataclass(frozen=True)
class HostToolchain:
    identifier: str
    name: str
    family: str
    cc: Path
    cxx: Path
    version: str | None = None
    source: str = "PATH"
    environment_script: Path | None = None
    environment_arguments: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        version = f" {self.version}" if self.version else ""
        return f"{self.name}{version} [{self.family}] - {self.cc}"

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["cc"] = str(self.cc)
        data["cxx"] = str(self.cxx)
        data["environment_script"] = str(self.environment_script) if self.environment_script else None
        data["environment_arguments"] = list(self.environment_arguments)
        return data


def _safe_identifier(value: str) -> str:
    return _SAFE_ID_RE.sub("-", value.lower()).strip("-") or "toolchain"


def _run_probe(command: list[str], env: Mapping[str, str] | None = None) -> str:
    return probe_text(command, env=env)


def _compiler_usable(
    cc: Path,
    cxx: Path,
    family: str,
    env: Mapping[str, str] | None = None,
) -> bool:
    with tempfile.TemporaryDirectory(prefix="llvm-manager-host-check-") as temporary:
        root = Path(temporary)
        c_source = root / "check.c"
        cxx_source = root / "check.cpp"
        executable_suffix = ".exe" if os.name == "nt" else ""
        c_output = root / f"check-c{executable_suffix}"
        cxx_output = root / f"check-cxx{executable_suffix}"
        c_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        cxx_source.write_text(
            "#include <vector>\nint main() { std::vector<int> values{0}; return values[0]; }\n",
            encoding="utf-8",
        )

        if family in {"msvc", "clang-cl"}:
            commands = [
                [str(cc), "/nologo", str(c_source), f"/Fe:{c_output}"],
                [str(cxx), "/nologo", str(cxx_source), f"/Fe:{cxx_output}"],
            ]
        else:
            commands = [
                [str(cc), str(c_source), "-o", str(c_output)],
                [str(cxx), str(cxx_source), "-o", str(cxx_output)],
            ]

        compiler_environment = dict(env) if env is not None else None
        for command in commands:
            completed = probe_process(command, cwd=root, env=compiler_environment, timeout=30)
            if completed is None or completed.returncode != 0:
                return False

        for output in (c_output, cxx_output):
            if not output.is_file():
                return False
            completed = probe_process([output], cwd=root, env=compiler_environment, timeout=10)
            if completed is None or completed.returncode != 0:
                return False
        return True


def _compiler_description(path: Path, env: Mapping[str, str] | None = None) -> tuple[str, str | None, str]:
    basename = path.name.lower()
    output = _run_probe([str(path), "--version"], env)
    if basename in {"cl", "cl.exe"}:
        output = _run_probe([str(path)], env) or output

    lowered = output.lower()
    if "apple clang" in lowered:
        family = "apple-clang"
        name = "AppleClang"
    elif basename.startswith("clang-cl"):
        family = "clang-cl"
        name = "ClangCL"
    elif "clang" in lowered or basename.startswith("clang"):
        family = "clang"
        name = "Clang"
    elif basename in {"cl", "cl.exe"} or "microsoft" in lowered:
        family = "msvc"
        name = "MSVC"
    elif "gcc" in lowered or "free software foundation" in lowered or basename.startswith(("gcc", "g++")):
        family = "gcc"
        name = "GCC"
    else:
        family = "unknown"
        name = path.stem

    return family, extract_numeric_version(output), name


def _is_compiler_executable_name(name: str) -> bool:
    normalized = name.lower()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return _COMPILER_EXECUTABLE_RE.fullmatch(normalized) is not None


def _iter_path_executables(search_path: str) -> Iterable[Path]:
    seen: set[Path] = set()
    for raw_directory in search_path.split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory).expanduser()
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name.lower())
        except OSError:
            continue
        for entry in entries:
            # PATH directories can contain protected or broken entries unrelated to
            # compilers. Filter by name before performing any operation that follows
            # the entry or reads its metadata.
            if not _is_compiler_executable_name(entry.name):
                continue
            absolute = entry.absolute()
            if absolute in seen:
                continue
            try:
                if not entry.is_file():
                    continue
                if os.name != "nt" and not os.access(entry, os.X_OK):
                    continue
            except OSError:
                continue
            seen.add(absolute)
            yield absolute


def _normalized_executable_name(path: Path) -> str:
    name = path.name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _pair_for_c_compiler(path: Path, by_name: dict[str, list[Path]]) -> Path | None:
    name = _normalized_executable_name(path)
    candidates: list[str] = []
    if name == "cc":
        candidates.append("c++")
    elif name == "cl":
        return path
    elif name == "clang-cl":
        return path
    elif match := re.fullmatch(r"clang(-\d+(?:\.\d+)*)?", name):
        candidates.append(f"clang++{match.group(1) or ''}")
    elif match := re.fullmatch(r"gcc(-\d+(?:\.\d+)*)?", name):
        candidates.append(f"g++{match.group(1) or ''}")
    else:
        return None

    for candidate in candidates:
        matches = by_name.get(candidate, [])
        same_directory = [match for match in matches if match.parent == path.parent]
        if same_directory:
            return same_directory[0]
    return None


def _path_toolchains(search_path: str) -> list[HostToolchain]:
    executables = list(_iter_path_executables(search_path))
    by_name: dict[str, list[Path]] = {}
    for executable in executables:
        by_name.setdefault(_normalized_executable_name(executable), []).append(executable)

    c_pattern = re.compile(r"(?:cc|cl|clang-cl|clang(?:-\d+(?:\.\d+)*)?|gcc(?:-\d+(?:\.\d+)*)?)$")
    toolchains: list[HostToolchain] = []
    seen_pairs: set[tuple[Path, Path]] = set()
    candidates = sorted(
        executables,
        key=lambda path: (
            _normalized_executable_name(path) == "cc",
            _normalized_executable_name(path),
            str(path),
        ),
    )
    for executable in candidates:
        name = _normalized_executable_name(executable)
        if not c_pattern.fullmatch(name):
            continue
        cxx = _pair_for_c_compiler(executable, by_name)
        if cxx is None:
            continue
        try:
            cc_resolved = executable.resolve()
            cxx_resolved = cxx.resolve()
        except OSError:
            cc_resolved = executable.absolute()
            cxx_resolved = cxx.absolute()
        pair = (cc_resolved, cxx_resolved)
        if pair in seen_pairs:
            continue

        family, version, display_name = _compiler_description(executable)
        if family == "unknown" or not _compiler_usable(executable, cxx, family):
            continue
        seen_pairs.add(pair)
        identifier_parts = [family, version or executable.stem]
        identifier = _safe_identifier("-".join(identifier_parts))
        toolchains.append(
            HostToolchain(
                identifier=identifier,
                name=display_name,
                family=family,
                cc=executable.absolute(),
                cxx=cxx.absolute(),
                version=version,
            )
        )
    return toolchains


def _find_vswhere() -> Path | None:
    found = shutil_which("vswhere.exe") or shutil_which("vswhere")
    if found:
        return Path(found).resolve()
    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not program_files:
        return None
    candidate = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    return candidate if candidate.is_file() else None


def _msvc_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86", "i386", "i686"}:
        return "x86"
    return "x64"


def _load_batch_environment(script: Path, arguments: tuple[str, ...]) -> dict[str, str]:
    command = f'call "{script}" {" ".join(arguments)} >nul && set'
    completed = probe_process(["cmd.exe", "/d", "/s", "/c", command], timeout=30)
    if completed is None or completed.returncode != 0:
        detail = completed.stderr.strip() if completed is not None else "process could not be started"
        raise LLVMManagerError(f"Could not initialize the MSVC environment from {script}: {detail}")

    environment = os.environ.copy()
    for line in completed.stdout.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        key, value = line.split("=", 1)
        environment[key] = value
    return environment


def _visual_studio_toolchains() -> list[HostToolchain]:
    if os.name != "nt":
        return []
    vswhere = _find_vswhere()
    if vswhere is None:
        return []
    completed = probe_process(
        [
            vswhere,
            "-all",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-format",
            "json",
            "-utf8",
        ],
        timeout=20,
    )
    if completed is None or completed.returncode != 0:
        return []
    try:
        instances = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []


    results: list[HostToolchain] = []
    architecture = _msvc_architecture()
    for instance in instances:
        root = Path(instance.get("installationPath", ""))
        script = root / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
        if not script.is_file():
            continue
        arguments = (architecture,)
        try:
            environment = _load_batch_environment(script, arguments)
        except LLVMManagerError:
            continue
        compiler = shutil_which("cl.exe", environment.get("PATH"))
        if not compiler:
            continue
        compiler_path = Path(compiler).resolve()
        _, compiler_version, _ = _compiler_description(compiler_path, environment)
        if not _compiler_usable(compiler_path, compiler_path, "msvc", environment):
            continue
        display_name = instance.get("displayName") or "Visual Studio MSVC"
        installation_version = instance.get("installationVersion")
        version = compiler_version or installation_version
        identifier = _safe_identifier(f"msvc-{installation_version or version or root.name}")
        results.append(
            HostToolchain(
                identifier=identifier,
                name=display_name,
                family="msvc",
                cc=compiler_path,
                cxx=compiler_path,
                version=version,
                source="Visual Studio",
                environment_script=script,
                environment_arguments=arguments,
            )
        )
    return results


def _xcrun_toolchains() -> list[HostToolchain]:
    if sys.platform != "darwin":
        return []
    xcrun = shutil_which("xcrun")
    if not xcrun:
        return []

    paths: dict[str, Path] = {}
    for language, compiler in (("cc", "clang"), ("cxx", "clang++")):
        completed = probe_process([xcrun, "--find", compiler], timeout=10)
        if completed is None or completed.returncode != 0:
            return []
        candidate = Path(completed.stdout.strip())
        if not candidate.is_file():
            return []
        paths[language] = candidate.absolute()

    family, version, display_name = _compiler_description(paths["cc"])
    if family != "apple-clang" or not _compiler_usable(paths["cc"], paths["cxx"], family):
        return []
    return [
        HostToolchain(
            identifier=_safe_identifier(f"{family}-{version or 'xcrun'}"),
            name=display_name,
            family=family,
            cc=paths["cc"],
            cxx=paths["cxx"],
            version=version,
            source="xcrun",
        )
    ]


def discover_host_toolchains(search_path: str | None = None) -> list[HostToolchain]:
    path_value = search_path if search_path is not None else os.environ.get("PATH", "")
    platform_toolchains: list[HostToolchain] = []
    if search_path is None:
        platform_toolchains = _visual_studio_toolchains() + _xcrun_toolchains()
    discovered = platform_toolchains + _path_toolchains(path_value)

    unique: list[HostToolchain] = []
    seen: set[tuple[Path, Path]] = set()
    for toolchain in discovered:
        try:
            pair = (toolchain.cc.resolve(), toolchain.cxx.resolve())
        except OSError:
            pair = (toolchain.cc.absolute(), toolchain.cxx.absolute())
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(toolchain)

    if os.name == "nt":
        family_order = {"msvc": 0, "clang-cl": 1, "clang": 2, "gcc": 3, "apple-clang": 4}
    elif sys.platform == "darwin":
        family_order = {"apple-clang": 0, "clang": 1, "gcc": 2, "clang-cl": 3, "msvc": 4}
    else:
        family_order = {"clang": 0, "gcc": 1, "apple-clang": 2, "clang-cl": 3, "msvc": 4}

    def version_key(version: str | None) -> tuple[int, int, int, int]:
        parts = [int(part) for part in (version or "0").split(".") if part.isdigit()]
        padded = (parts + [0, 0, 0, 0])[:4]
        return tuple(-part for part in padded)

    ordered = sorted(
        unique,
        key=lambda item: (
            family_order.get(item.family, 99),
            version_key(item.version),
            item.name.lower(),
            str(item.cc),
        ),
    )

    totals: dict[str, int] = {}
    for toolchain in ordered:
        totals[toolchain.identifier] = totals.get(toolchain.identifier, 0) + 1
    occurrences: dict[str, int] = {}
    result: list[HostToolchain] = []
    for toolchain in ordered:
        if totals[toolchain.identifier] == 1:
            result.append(toolchain)
            continue
        occurrence = occurrences.get(toolchain.identifier, 0) + 1
        occurrences[toolchain.identifier] = occurrence
        result.append(replace(toolchain, identifier=f"{toolchain.identifier}-{occurrence}"))
    return result


def toolchain_environment(toolchain: HostToolchain) -> dict[str, str]:
    if toolchain.environment_script:
        return _load_batch_environment(toolchain.environment_script, toolchain.environment_arguments)
    return os.environ.copy()


def print_host_toolchains(toolchains: list[HostToolchain]) -> None:
    # Compatibility wrapper; terminal rendering lives in presentation.py.
    from .presentation import print_host_toolchains as render

    render(toolchains)


def select_host_toolchain(toolchains: list[HostToolchain], selector: str) -> HostToolchain:
    selection = selector.strip()
    if selection.isdigit():
        index = int(selection)
        if 1 <= index <= len(toolchains):
            return toolchains[index - 1]

    lowered = selection.lower()
    exact = [
        toolchain
        for toolchain in toolchains
        if lowered
        in {
            toolchain.identifier.lower(),
            toolchain.name.lower(),
            str(toolchain.cc).lower(),
            str(toolchain.cxx).lower(),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise LLVMManagerError(
            f"Multiple toolchains match {selector!r}; select one by list number or unique ID"
        )

    family = [toolchain for toolchain in toolchains if toolchain.family.lower() == lowered]
    if len(family) == 1:
        return family[0]
    if len(family) > 1:
        raise LLVMManagerError(
            f"Multiple {selection} toolchains were found; select one by list number or ID"
        )
    raise LLVMManagerError(f"No host compiler toolchain matches {selector!r}")


def prompt_for_host_toolchain(
    toolchains: list[HostToolchain], *, display: bool = True
) -> HostToolchain:
    if not toolchains:
        raise LLVMManagerError(
            "No usable host C/C++ compiler was found. Install GCC, Clang/AppleClang, "
            "or the MSVC C++ build tools and try again."
        )
    if display:
        print_host_toolchains(toolchains)
    default = "1"
    while True:
        try:
            selection = input(f"Compiler toolchain to use [{default}]: ").strip() or default
        except EOFError as error:
            raise LLVMManagerError(
                "A compiler selection is required; rerun with --toolchain <number-or-id>"
            ) from error
        try:
            return select_host_toolchain(toolchains, selection)
        except LLVMManagerError as error:
            print(error)
