from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .aliases import ensure_versioned_binaries
from .config import ManagerPaths
from .repository import DEFAULT_REPOSITORY_URL, LLVMRepository, SourceRevision
from .standard_library import (
    MANAGED_LIBCXX_STANDARD_LIBRARY,
    SYSTEM_CXX_STANDARD_LIBRARY,
    configure_standard_library,
    managed_libcxx_capability,
    standard_library_cmake_options,
    standard_library_runtimes,
    validate_standard_library,
)
from .toolchains import HostToolchain, toolchain_environment
from .util import LLVMManagerError, file_lock, read_json, require_tools, run, write_json

_LLVM_MAJOR_RE = re.compile(r"\bset\s*\(\s*LLVM_VERSION_MAJOR\s+(\d+)\s*\)", re.IGNORECASE)
_BUILD_STATE_NAME = ".llvm-manager-build.json"


@dataclass(frozen=True)
class BuildOptions:
    revision: SourceRevision
    install_prefix: Path
    host_toolchain: HostToolchain
    build_type: str = "Release"
    projects: tuple[str, ...] = ("clang", "clang-tools-extra", "lld")
    runtimes: tuple[str, ...] = ("compiler-rt",)
    targets: str = "Native"
    jobs: int = max(1, os.cpu_count() or 1)
    repository_url: str = DEFAULT_REPOSITORY_URL
    cxx_standard_library: str = SYSTEM_CXX_STANDARD_LIBRARY
    verify: bool = True
    clean: bool = False


def _cmake_list(values: tuple[str, ...]) -> str:
    return ";".join(value for value in values if value)


def _effective_runtimes(options: BuildOptions) -> tuple[str, ...]:
    return standard_library_runtimes(options.cxx_standard_library, options.runtimes)


def _host_cmake_options(platform: str) -> tuple[str, ...]:
    if platform == "darwin":
        return ("-DCLANG_USE_XCSELECT=ON",)
    return ()


def _validate_options(options: BuildOptions) -> None:
    if options.jobs < 1:
        raise LLVMManagerError("Build job count must be at least 1")
    if options.build_type not in {"Debug", "Release", "RelWithDebInfo", "MinSizeRel"}:
        raise LLVMManagerError(f"Unsupported CMake build type: {options.build_type}")
    if "clang" not in options.projects:
        raise LLVMManagerError("The project list must include clang")
    validate_standard_library(options.cxx_standard_library)
    if not options.host_toolchain.cc.is_file():
        raise LLVMManagerError(f"Selected C compiler was not found: {options.host_toolchain.cc}")
    if not options.host_toolchain.cxx.is_file():
        raise LLVMManagerError(f"Selected C++ compiler was not found: {options.host_toolchain.cxx}")


def _llvm_major(llvm_source: Path) -> int:
    candidates = (
        llvm_source.parent / "cmake" / "Modules" / "LLVMVersion.cmake",
        llvm_source / "CMakeLists.txt",
        llvm_source / "cmake" / "Modules" / "LLVMVersion.cmake",
    )
    for candidate in candidates:
        try:
            contents = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        match = _LLVM_MAJOR_RE.search(contents)
        if match:
            return int(match.group(1))

    searched = ", ".join(str(candidate) for candidate in candidates)
    raise LLVMManagerError(
        "Could not determine LLVM_VERSION_MAJOR from the selected source revision. "
        f"Searched: {searched}"
    )


def _configuration(options: BuildOptions, install_prefix: Path) -> dict[str, object]:
    return {
        "revision": {"kind": options.revision.kind.value, "value": options.revision.value},
        "install_prefix": str(install_prefix),
        "build_type": options.build_type,
        "projects": list(options.projects),
        "runtimes": list(_effective_runtimes(options)),
        "cxx_standard_library": options.cxx_standard_library,
        "targets": options.targets,
        "repository": options.repository_url,
        "host_cc": str(options.host_toolchain.cc),
        "host_cxx": str(options.host_toolchain.cxx),
    }


def _configuration_hash(configuration: dict[str, object]) -> str:
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_build_directory(build_dir: Path, configuration: dict[str, object], clean: bool) -> None:
    state_file = build_dir / _BUILD_STATE_NAME
    previous = read_json(state_file, {})
    changed = not isinstance(previous, dict) or previous.get("configuration") != configuration
    if build_dir.exists() and (clean or changed):
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    write_json(state_file, {"configuration": configuration})


def _remove_recorded_install_files(prefix: Path, metadata: dict[str, object]) -> None:
    installed = metadata.get("installed_files")
    if not isinstance(installed, list):
        return
    parents: set[Path] = set()
    for value in installed:
        if not isinstance(value, str):
            continue
        candidate = (prefix / value).resolve()
        try:
            candidate.relative_to(prefix)
        except ValueError:
            continue
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink()
            parents.add(candidate.parent)
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        while parent != prefix:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _prepare_install_prefix(paths: ManagerPaths, prefix: Path, config_hash: str, clean: bool) -> None:
    if not prefix.exists():
        prefix.parent.mkdir(parents=True, exist_ok=True)
        return
    metadata_path = prefix / ".llvm-manager.json"
    metadata_value = read_json(metadata_path, {}, warn=True)
    metadata = metadata_value if isinstance(metadata_value, dict) else {}

    if clean:
        managed_location = False
        try:
            prefix.relative_to(paths.install_root.resolve())
            managed_location = True
        except ValueError:
            pass
        if not metadata_path.is_file() and not managed_location:
            raise LLVMManagerError(
                f"Refusing to clean an unmanaged custom install directory: {prefix}"
            )
        shutil.rmtree(prefix)
        prefix.mkdir(parents=True, exist_ok=True)
        return

    if metadata and metadata.get("configuration_hash") != config_hash:
        _remove_recorded_install_files(prefix, metadata)


def _configure_command(
    cmake: str,
    ninja: str,
    llvm_source: Path,
    build_dir: Path,
    install_prefix: Path,
    options: BuildOptions,
) -> list[str | Path]:
    command: list[str | Path] = [
        cmake,
        "-S",
        llvm_source,
        "-B",
        build_dir,
        "-G",
        "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-DCMAKE_BUILD_TYPE={options.build_type}",
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        f"-DCMAKE_C_COMPILER={options.host_toolchain.cc}",
        f"-DCMAKE_CXX_COMPILER={options.host_toolchain.cxx}",
        f"-DLLVM_ENABLE_PROJECTS={_cmake_list(options.projects)}",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
    ]
    command.extend(_host_cmake_options(sys.platform))
    runtimes = _effective_runtimes(options)
    if runtimes:
        command.append(f"-DLLVM_ENABLE_RUNTIMES={_cmake_list(runtimes)}")
    command.extend(standard_library_cmake_options(options.cxx_standard_library))
    if options.targets and options.targets.lower() != "all":
        command.append(f"-DLLVM_TARGETS_TO_BUILD={options.targets}")
    return command


def _verify_install(
    prefix: Path,
    major: int,
    env: dict[str, str],
    *,
    run_executables: bool,
) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    clang = prefix / "bin" / f"clang-{major}{suffix}"
    clangxx = prefix / "bin" / f"clang++-{major}{suffix}"
    if not clang.is_file():
        raise LLVMManagerError(f"Installed compiler was not found: {clang}")
    if not clangxx.is_file():
        raise LLVMManagerError(f"Installed C++ compiler was not found: {clangxx}")
    run([clang, "--version"], env=env)

    with tempfile.TemporaryDirectory(prefix="llvm-manager-verify-") as temporary:
        directory = Path(temporary)
        executable_suffix = ".exe" if os.name == "nt" else ""
        programs = (
            (
                clang,
                directory / "verify.c",
                directory / f"verify-c{executable_suffix}",
                "int main(void) { return 0; }\n",
                (),
            ),
            (
                clangxx,
                directory / "verify.cxx",
                directory / f"verify-cxx{executable_suffix}",
                "#include <concepts>\nstatic_assert(std::same_as<int, int>);\nint main() { return 0; }\n",
                ("-std=c++20",),
            ),
        )
        for compiler, source, output, contents, flags in programs:
            source.write_text(contents, encoding="utf-8")
            run([compiler, *flags, source, "-o", output], env=env)
            if not output.is_file():
                raise LLVMManagerError(f"Compiler verification produced no executable: {output}")
            if run_executables:
                run([output], env=env)


def _installed_files(
    build_dir: Path,
    prefix: Path,
    manager_files: list[Path],
) -> list[str]:
    paths = [*manager_files, prefix / ".llvm-manager.json"]
    manifest = build_dir / "install_manifest.txt"
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                paths.append(Path(line.strip()))

    result: set[str] = set()
    for path in paths:
        try:
            result.add(str(path.resolve().relative_to(prefix)))
        except (OSError, ValueError):
            continue
    return sorted(result)


def build_and_install(paths: ManagerPaths, options: BuildOptions) -> Path:
    _validate_options(options)
    tools = require_tools(("git", "cmake", "ninja"))
    build_env = toolchain_environment(options.host_toolchain)
    paths.ensure_root()

    with file_lock(paths.lock_file, timeout=60):
        paths.ensure_build_layout()
        repository = LLVMRepository(paths.repository, options.repository_url, git=tools["git"])
        resolved_revision = repository.checkout(options.revision)

        llvm_source = paths.repository / "llvm"
        if not (llvm_source / "CMakeLists.txt").is_file():
            raise LLVMManagerError(
                f"Selected {options.revision.label} does not contain the expected monorepo LLVM CMake project: {llvm_source}"
            )
        major = _llvm_major(llvm_source)

        build_dir = paths.build_root / (
            f"{options.revision.directory_name}-{options.build_type}-{options.host_toolchain.identifier}"
        )
        install_prefix = options.install_prefix.expanduser().resolve()
        configuration = _configuration(options, install_prefix)
        config_hash = _configuration_hash(configuration)
        _prepare_build_directory(build_dir, configuration, options.clean)
        _prepare_install_prefix(paths, install_prefix, config_hash, options.clean)

        run(
            _configure_command(
                tools["cmake"],
                tools["ninja"],
                llvm_source,
                build_dir,
                install_prefix,
                options,
            ),
            env=build_env,
        )
        run(
            [
                tools["cmake"],
                "--build",
                build_dir,
                "--target",
                "install",
                "--parallel",
                str(options.jobs),
            ],
            env=build_env,
        )

        created_aliases = ensure_versioned_binaries(install_prefix, major)
        standard_library, standard_library_files = configure_standard_library(
            install_prefix,
            major,
            build_env,
            options.cxx_standard_library,
        )
        manager_files = [*created_aliases, *standard_library_files]
        if options.verify:
            _verify_install(
                install_prefix,
                major,
                build_env,
                run_executables=options.targets.strip().lower() == "native",
            )

        metadata = {
            "schema_version": 4,
            "source": resolved_revision.to_json(),
            "major": major,
            "configuration_hash": config_hash,
            "build_type": options.build_type,
            "projects": list(options.projects),
            "runtimes": list(_effective_runtimes(options)),
            "cxx_standard_library": standard_library,
            "targets": options.targets,
            "repository": options.repository_url,
            "host_toolchain": options.host_toolchain.to_json(),
            "versioned_aliases": [str(path.relative_to(install_prefix)) for path in created_aliases],
        }
        if standard_library.get("kind") == MANAGED_LIBCXX_STANDARD_LIBRARY:
            metadata["managed_cxx_standard_library"] = managed_libcxx_capability(standard_library)
        metadata["installed_files"] = _installed_files(build_dir, install_prefix, manager_files)
        write_json(install_prefix / ".llvm-manager.json", metadata)
        return install_prefix
