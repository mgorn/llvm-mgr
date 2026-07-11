from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .aliases import ensure_versioned_binaries
from .config import ManagerPaths
from .repository import (
    DEFAULT_REPOSITORY_URL,
    LLVMRepository,
    RevisionKind,
    SourceRevision,
)
from .toolchains import HostToolchain, toolchain_environment
from .util import LLVMManagerError, require_tools, run, write_json
from .versioning import parse_llvm_tag

_LLVM_MAJOR_RE = re.compile(r"\bset\s*\(\s*LLVM_VERSION_MAJOR\s+(\d+)\s*\)", re.IGNORECASE)


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
    verify: bool = True


def _cmake_list(values: tuple[str, ...]) -> str:
    return ";".join(value for value in values if value)


def _validate_options(options: BuildOptions) -> None:
    if options.revision.kind is RevisionKind.TAG and parse_llvm_tag(options.revision.value) is None:
        raise LLVMManagerError(f"Not a recognized LLVM release tag: {options.revision.value}")
    if options.jobs < 1:
        raise LLVMManagerError("Build job count must be at least 1")
    if options.build_type not in {"Debug", "Release", "RelWithDebInfo", "MinSizeRel"}:
        raise LLVMManagerError(f"Unsupported CMake build type: {options.build_type}")
    if "clang" not in options.projects:
        raise LLVMManagerError("The project list must include clang")
    if not options.host_toolchain.cc.is_file():
        raise LLVMManagerError(f"Selected C compiler was not found: {options.host_toolchain.cc}")
    if not options.host_toolchain.cxx.is_file():
        raise LLVMManagerError(f"Selected C++ compiler was not found: {options.host_toolchain.cxx}")


def _llvm_major(llvm_source: Path) -> int:
    # Modern LLVM keeps the version definition in the monorepo-level CMake
    # modules directory. Keep the other locations as fallbacks for older or
    # downstream source layouts.
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


def _verify_install(prefix: Path, major: int, env: dict[str, str]) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    clang = prefix / "bin" / f"clang-{major}{suffix}"
    if not clang.is_file():
        raise LLVMManagerError(f"Installed compiler was not found: {clang}")
    run([clang, "--version"], env=env)
    with tempfile.TemporaryDirectory(prefix="llvm-manager-verify-") as temporary:
        source = Path(temporary) / "verify.c"
        output = Path(temporary) / ("verify.obj" if os.name == "nt" else "verify.o")
        source.write_text("int llvm_manager_verify(void) { return 0; }\n", encoding="utf-8")
        run([clang, "-c", source, "-o", output], env=env)
        if not output.is_file():
            raise LLVMManagerError("Clang verification command succeeded but produced no object file")


def build_and_install(paths: ManagerPaths, options: BuildOptions) -> Path:
    _validate_options(options)
    tools = require_tools(["git", "cmake", "ninja"])
    build_env = toolchain_environment(options.host_toolchain)
    paths.ensure()

    repository = LLVMRepository(paths.repository, options.repository_url)
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
    build_dir.mkdir(parents=True, exist_ok=True)
    install_prefix.parent.mkdir(parents=True, exist_ok=True)

    configure = [
        tools["cmake"],
        "-S",
        llvm_source,
        "-B",
        build_dir,
        "-G",
        "Ninja",
        f"-DCMAKE_BUILD_TYPE={options.build_type}",
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        f"-DCMAKE_C_COMPILER={options.host_toolchain.cc}",
        f"-DCMAKE_CXX_COMPILER={options.host_toolchain.cxx}",
        f"-DLLVM_ENABLE_PROJECTS={_cmake_list(options.projects)}",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
    ]
    if options.runtimes:
        configure.append(f"-DLLVM_ENABLE_RUNTIMES={_cmake_list(options.runtimes)}")
    if options.targets and options.targets.lower() != "all":
        configure.append(f"-DLLVM_TARGETS_TO_BUILD={options.targets}")

    run(configure, env=build_env)
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
    if options.verify:
        _verify_install(install_prefix, major, build_env)

    metadata = {
        "source": resolved_revision.to_json(),
        "major": major,
        "build_type": options.build_type,
        "projects": list(options.projects),
        "runtimes": list(options.runtimes),
        "targets": options.targets,
        "repository": options.repository_url,
        "host_toolchain": options.host_toolchain.to_json(),
        "versioned_aliases": [str(path.relative_to(install_prefix)) for path in created_aliases],
    }
    if options.revision.kind is RevisionKind.TAG:
        metadata["tag"] = options.revision.value
    write_json(install_prefix / ".llvm-manager.json", metadata)
    return install_prefix
