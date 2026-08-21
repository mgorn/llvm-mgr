from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .aliases import ensure_versioned_binaries, ensure_windows_mt_alias
from .config import ManagerPaths
from .repository import DEFAULT_REPOSITORY_URL, LLVMRepository, SourceRevision
from .standard_library import (
    MACOS_MANAGED_LIBCXX_ARCHITECTURES,
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
_LLVM_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_BUILD_STATE_NAME = ".llvm-manager-build.json"
_ALL_LLVM_TOOLS = "all"
_WINDOWS_LIBXML2_VERSION = "2.9.12"
_WINDOWS_LIBXML2_SHA256 = "98bfa7a9a5e2a75638422050740448ee9f02bf4dc2075c9822d7747d5ff9e617"
_WINDOWS_LIBXML2_URL = (
    "https://gitlab.gnome.org/GNOME/libxml2/-/archive/"
    f"v{_WINDOWS_LIBXML2_VERSION}/libxml2-v{_WINDOWS_LIBXML2_VERSION}.tar.gz"
)
_WINDOWS_LIBXML2_CMAKE_OPTIONS = (
    "-DBUILD_SHARED_LIBS=OFF",
    "-DLIBXML2_WITH_C14N=OFF",
    "-DLIBXML2_WITH_CATALOG=OFF",
    "-DLIBXML2_WITH_DEBUG=OFF",
    "-DLIBXML2_WITH_DOCB=OFF",
    "-DLIBXML2_WITH_FTP=OFF",
    "-DLIBXML2_WITH_HTML=OFF",
    "-DLIBXML2_WITH_HTTP=OFF",
    "-DLIBXML2_WITH_ICONV=OFF",
    "-DLIBXML2_WITH_ICU=OFF",
    "-DLIBXML2_WITH_ISO8859X=OFF",
    "-DLIBXML2_WITH_LEGACY=OFF",
    "-DLIBXML2_WITH_LZMA=OFF",
    "-DLIBXML2_WITH_MEM_DEBUG=OFF",
    "-DLIBXML2_WITH_MODULES=OFF",
    "-DLIBXML2_WITH_OUTPUT=ON",
    "-DLIBXML2_WITH_PATTERN=OFF",
    "-DLIBXML2_WITH_PROGRAMS=OFF",
    "-DLIBXML2_WITH_PUSH=OFF",
    "-DLIBXML2_WITH_PYTHON=OFF",
    "-DLIBXML2_WITH_READER=OFF",
    "-DLIBXML2_WITH_REGEXPS=OFF",
    "-DLIBXML2_WITH_RUN_DEBUG=OFF",
    "-DLIBXML2_WITH_SAX1=ON",
    "-DLIBXML2_WITH_SCHEMAS=OFF",
    "-DLIBXML2_WITH_SCHEMATRON=OFF",
    "-DLIBXML2_WITH_TESTS=OFF",
    "-DLIBXML2_WITH_THREADS=ON",
    "-DLIBXML2_WITH_THREAD_ALLOC=OFF",
    "-DLIBXML2_WITH_TREE=ON",
    "-DLIBXML2_WITH_VALID=OFF",
    "-DLIBXML2_WITH_WRITER=OFF",
    "-DLIBXML2_WITH_XINCLUDE=OFF",
    "-DLIBXML2_WITH_XPATH=OFF",
    "-DLIBXML2_WITH_XPTR=OFF",
    "-DLIBXML2_WITH_ZLIB=OFF",
    "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded",
)


@dataclass(frozen=True)
class WindowsLibXml2:
    include_dir: Path
    library: Path


@dataclass(frozen=True)
class BuildOptions:
    revision: SourceRevision
    install_prefix: Path
    host_toolchain: HostToolchain
    build_type: str = "Release"
    projects: tuple[str, ...] = ("clang", "clang-tools-extra", "lld")
    runtimes: tuple[str, ...] = ("compiler-rt",)
    tools: tuple[str, ...] = (_ALL_LLVM_TOOLS,)
    targets: str = "all"
    jobs: int = max(1, os.cpu_count() or 1)
    repository_url: str = DEFAULT_REPOSITORY_URL
    cxx_standard_library: str = SYSTEM_CXX_STANDARD_LIBRARY
    verify: bool = True
    clean: bool = False


def _cmake_list(values: tuple[str, ...]) -> str:
    return ";".join(value for value in values if value)


def _normalized_tools(tools: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_name in tools:
        name = raw_name.strip()
        if not name:
            continue
        if name.lower() == _ALL_LLVM_TOOLS:
            name = _ALL_LLVM_TOOLS
        elif name.lower() == "mt":
            name = "llvm-mt"
        if not _LLVM_TOOL_NAME_RE.fullmatch(name):
            raise LLVMManagerError(f"Invalid LLVM tool name: {raw_name!r}")
        if name not in normalized:
            normalized.append(name)

    if not normalized:
        raise LLVMManagerError("Select at least one LLVM tool, or use 'all'")
    if _ALL_LLVM_TOOLS in normalized:
        if len(normalized) != 1:
            raise LLVMManagerError("LLVM tool selection 'all' cannot be combined with individual tools")
        return (_ALL_LLVM_TOOLS,)

    if "clang" not in normalized:
        normalized.insert(0, "clang")
    return tuple(normalized)


def _builds_all_tools(options: BuildOptions) -> bool:
    return _normalized_tools(options.tools) == (_ALL_LLVM_TOOLS,)


def _distribution_components(options: BuildOptions) -> tuple[str, ...]:
    tools = _normalized_tools(options.tools)
    if tools == (_ALL_LLVM_TOOLS,):
        return ()

    components: list[str] = []
    for tool in tools:
        components.append(tool)
        if tool == "clang":
            components.append("clang-resource-headers")
    return tuple(dict.fromkeys(components))


def _install_targets(options: BuildOptions, platform: str) -> tuple[str, ...]:
    if _builds_all_tools(options):
        return ("install",)

    targets = ["install-distribution"]
    if _primary_build_runtimes(options, platform):
        targets.append("install-runtimes")
    return tuple(targets)


def _tool_selection_requires_libxml2(options: BuildOptions, platform: str) -> bool:
    if not platform.startswith("win"):
        return False
    tools = _normalized_tools(options.tools)
    return tools == (_ALL_LLVM_TOOLS,) or "llvm-mt" in tools


def _verification_tools(options: BuildOptions, platform: str) -> tuple[str, ...]:
    tools = _normalized_tools(options.tools)
    if tools == (_ALL_LLVM_TOOLS,):
        return ("llvm-mt", "mt") if platform.startswith("win") else ()
    if platform.startswith("win") and "llvm-mt" in tools:
        return (*tools, "mt")
    return tools


def _effective_runtimes(options: BuildOptions) -> tuple[str, ...]:
    return standard_library_runtimes(options.cxx_standard_library, options.runtimes)


def _primary_build_runtimes(options: BuildOptions, platform: str) -> tuple[str, ...]:
    runtimes = _effective_runtimes(options)
    if platform != "darwin" or options.cxx_standard_library != MANAGED_LIBCXX_STANDARD_LIBRARY:
        return runtimes
    managed = set(standard_library_runtimes(MANAGED_LIBCXX_STANDARD_LIBRARY, ()))
    return tuple(runtime for runtime in runtimes if runtime not in managed)


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
    _normalized_tools(options.tools)
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
        "tools": list(_normalized_tools(options.tools)),
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


def _download_verified_archive(url: str, destination: Path, sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest == sha256:
            return
        destination.unlink()

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "llvm-manager"})
    try:
        with urllib.request.urlopen(request) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if digest != sha256:
            raise LLVMManagerError(
                f"Downloaded archive checksum mismatch for {url}: expected {sha256}, got {digest}"
            )
        os.replace(temporary, destination)
    except OSError as error:
        raise LLVMManagerError(f"Could not download {url}: {error}") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_extract_tar_gz(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                try:
                    target.relative_to(root)
                except ValueError as error:
                    raise LLVMManagerError(
                        f"Refusing to extract unsafe archive member: {member.name}"
                    ) from error
                if member.isdev() or member.isfifo():
                    raise LLVMManagerError(
                        f"Refusing to extract unsupported archive member: {member.name}"
                    )
                if member.issym() or member.islnk():
                    link_base = target.parent if member.issym() else destination
                    link_target = (link_base / member.linkname).resolve()
                    try:
                        link_target.relative_to(root)
                    except ValueError as error:
                        raise LLVMManagerError(
                            f"Refusing to extract unsafe archive link: {member.name}"
                        ) from error
            archive.extractall(destination)
    except (OSError, tarfile.TarError) as error:
        raise LLVMManagerError(f"Could not extract {archive_path}: {error}") from error


def _windows_libxml2_source(paths: ManagerPaths) -> Path:
    source = paths.source_root / f"libxml2-v{_WINDOWS_LIBXML2_VERSION}"
    if (source / "CMakeLists.txt").is_file():
        return source

    archive_path = paths.source_root / f"libxml2-v{_WINDOWS_LIBXML2_VERSION}.tar.gz"
    _download_verified_archive(_WINDOWS_LIBXML2_URL, archive_path, _WINDOWS_LIBXML2_SHA256)
    temporary = paths.source_root / f".libxml2-extract-{uuid.uuid4().hex}"
    try:
        _safe_extract_tar_gz(archive_path, temporary)
        extracted = temporary / f"libxml2-v{_WINDOWS_LIBXML2_VERSION}"
        if not (extracted / "CMakeLists.txt").is_file():
            raise LLVMManagerError(
                f"libxml2 archive did not contain the expected source directory: {extracted}"
            )
        if source.exists():
            shutil.rmtree(source)
        shutil.move(str(extracted), str(source))
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return source


def _windows_libxml2_library(install_dir: Path) -> Path:
    candidates = (
        install_dir / "lib" / "libxml2s.lib",
        install_dir / "lib" / "libxml2.lib",
        install_dir / "lib" / "xml2.lib",
        install_dir / "lib" / "libxml2s.a",
        install_dir / "lib" / "libxml2.a",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = (
        sorted(
            path
            for pattern in ("*xml2*.lib", "*xml2*.a")
            for path in (install_dir / "lib").glob(pattern)
        )
        if (install_dir / "lib").is_dir()
        else []
    )
    if found:
        return found[0]
    raise LLVMManagerError(f"Managed libxml2 build produced no static library under {install_dir / 'lib'}")


def _build_windows_libxml2(
    paths: ManagerPaths,
    options: BuildOptions,
    cmake: str,
    ninja: str,
    env: dict[str, str],
) -> WindowsLibXml2 | None:
    if not _tool_selection_requires_libxml2(options, sys.platform):
        return None

    source = _windows_libxml2_source(paths)
    build_dir = (
        paths.build_root
        / "_dependencies"
        / f"libxml2-{_WINDOWS_LIBXML2_VERSION}-{options.host_toolchain.identifier}"
    )
    install_dir = build_dir / "install"
    include_dir = install_dir / "include" / "libxml2"
    if include_dir.is_dir():
        try:
            return WindowsLibXml2(include_dir, _windows_libxml2_library(install_dir))
        except LLVMManagerError:
            pass

    run(
        [
            cmake,
            "-S",
            source,
            "-B",
            build_dir,
            "-G",
            "Ninja",
            f"-DCMAKE_MAKE_PROGRAM={ninja}",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={install_dir}",
            f"-DCMAKE_C_COMPILER={options.host_toolchain.cc}",
            *_WINDOWS_LIBXML2_CMAKE_OPTIONS,
        ],
        env=env,
    )
    run(
        [
            cmake,
            "--build",
            build_dir,
            "--target",
            "install",
            "--parallel",
            str(options.jobs),
        ],
        env=env,
    )
    if not include_dir.is_dir():
        raise LLVMManagerError(f"Managed libxml2 build produced no headers under {include_dir}")
    return WindowsLibXml2(include_dir, _windows_libxml2_library(install_dir))


def _configure_command(
    cmake: str,
    ninja: str,
    llvm_source: Path,
    build_dir: Path,
    install_prefix: Path,
    options: BuildOptions,
    windows_libxml2: WindowsLibXml2 | None = None,
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
    runtimes = _primary_build_runtimes(options, sys.platform)
    if runtimes:
        command.append(f"-DLLVM_ENABLE_RUNTIMES={_cmake_list(runtimes)}")
    if sys.platform != "darwin" or options.cxx_standard_library != MANAGED_LIBCXX_STANDARD_LIBRARY:
        command.extend(standard_library_cmake_options(options.cxx_standard_library))
    components = _distribution_components(options)
    if components:
        command.append(f"-DLLVM_DISTRIBUTION_COMPONENTS={_cmake_list(components)}")
    if _tool_selection_requires_libxml2(options, sys.platform):
        if windows_libxml2 is None:
            raise LLVMManagerError("A managed libxml2 build is required to build llvm-mt on Windows")
        command.extend(
            (
                "-DLLVM_ENABLE_LIBXML2=FORCE_ON",
                "-DCLANG_ENABLE_LIBXML2=OFF",
                f"-DLIBXML2_INCLUDE_DIR={windows_libxml2.include_dir.as_posix()}",
                f"-DLIBXML2_LIBRARY={windows_libxml2.library.as_posix()}",
                f"-DLIBXML2_LIBRARIES={windows_libxml2.library.as_posix()}",
                "-DCMAKE_C_FLAGS=-DLIBXML_STATIC",
                "-DCMAKE_CXX_FLAGS=-DLIBXML_STATIC",
            )
        )
    if options.targets:
        command.append(f"-DLLVM_TARGETS_TO_BUILD={options.targets}")
    return command


def _macos_managed_libcxx_configure_command(
    cmake: str,
    ninja: str,
    runtimes_source: Path,
    build_dir: Path,
    install_prefix: Path,
    build_type: str,
) -> list[str | Path]:
    clang = install_prefix / "bin" / "clang"
    clangxx = install_prefix / "bin" / "clang++"
    return [
        cmake,
        "-S",
        runtimes_source,
        "-B",
        build_dir,
        "-G",
        "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-DCMAKE_BUILD_TYPE={build_type}",
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        f"-DCMAKE_C_COMPILER={clang}",
        f"-DCMAKE_CXX_COMPILER={clangxx}",
        f"-DCMAKE_ASM_COMPILER={clang}",
        "-DCMAKE_C_FLAGS=--no-default-config",
        "-DCMAKE_CXX_FLAGS=--no-default-config",
        "-DCMAKE_ASM_FLAGS=--no-default-config",
        f"-DCMAKE_OSX_ARCHITECTURES={_cmake_list(MACOS_MANAGED_LIBCXX_ARCHITECTURES)}",
        "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind",
        "-DLIBCXXABI_USE_LLVM_UNWINDER=ON",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLIBCXX_INCLUDE_TESTS=OFF",
        "-DLIBCXXABI_INCLUDE_TESTS=OFF",
        "-DLIBUNWIND_INCLUDE_TESTS=OFF",
    ]


def _build_macos_managed_libcxx(
    cmake: str,
    ninja: str,
    llvm_source: Path,
    build_dir: Path,
    install_prefix: Path,
    options: BuildOptions,
    env: dict[str, str],
) -> Path | None:
    if sys.platform != "darwin" or options.cxx_standard_library != MANAGED_LIBCXX_STANDARD_LIBRARY:
        return None
    runtimes_source = llvm_source.parent / "runtimes"
    if not (runtimes_source / "CMakeLists.txt").is_file():
        raise LLVMManagerError(f"Selected source revision has no runtimes CMake project: {runtimes_source}")
    clang = install_prefix / "bin" / "clang"
    clangxx = install_prefix / "bin" / "clang++"
    if not clang.is_file() or not clangxx.is_file():
        raise LLVMManagerError(
            "The just-built Clang installation is required before building the universal managed libc++ runtime"
        )

    runtime_build_dir = build_dir / "managed-libcxx-universal"
    run(
        _macos_managed_libcxx_configure_command(
            cmake,
            ninja,
            runtimes_source,
            runtime_build_dir,
            install_prefix,
            options.build_type,
        ),
        env=env,
    )
    run(
        [
            cmake,
            "--build",
            runtime_build_dir,
            "--target",
            "install",
            "--parallel",
            str(options.jobs),
        ],
        env=env,
    )
    return runtime_build_dir


def _verify_install(
    prefix: Path,
    major: int,
    env: dict[str, str],
    *,
    run_executables: bool,
    cxx_architectures: tuple[str, ...] = (),
    required_tools: tuple[str, ...] = (),
) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    clang = prefix / "bin" / f"clang-{major}{suffix}"
    clangxx = prefix / "bin" / f"clang++-{major}{suffix}"
    if not clang.is_file():
        raise LLVMManagerError(f"Installed compiler was not found: {clang}")
    if not clangxx.is_file():
        raise LLVMManagerError(f"Installed C++ compiler was not found: {clangxx}")
    for tool in required_tools:
        executable = prefix / "bin" / f"{tool}{suffix}"
        if not executable.is_file():
            raise LLVMManagerError(f"Requested LLVM tool was not installed: {executable}")
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

        for architecture in cxx_architectures:
            source = directory / f"verify-cxx-{architecture}.cxx"
            output = directory / f"verify-cxx-{architecture}{executable_suffix}"
            source.write_text(
                "#include <concepts>\nstatic_assert(std::same_as<int, int>);\nint main() { return 0; }\n",
                encoding="utf-8",
            )
            run([clangxx, "-std=c++20", "-arch", architecture, source, "-o", output], env=env)
            if not output.is_file():
                raise LLVMManagerError(
                    f"Compiler verification produced no {architecture} C++ executable: {output}"
                )


def _installed_files(
    build_dirs: tuple[Path, ...],
    prefix: Path,
    manager_files: list[Path],
) -> list[str]:
    paths = [*manager_files, prefix / ".llvm-manager.json"]
    for build_dir in build_dirs:
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
        windows_libxml2 = _build_windows_libxml2(
            paths,
            options,
            tools["cmake"],
            tools["ninja"],
            build_env,
        )

        run(
            _configure_command(
                tools["cmake"],
                tools["ninja"],
                llvm_source,
                build_dir,
                install_prefix,
                options,
                windows_libxml2,
            ),
            env=build_env,
        )
        for install_target in _install_targets(options, sys.platform):
            run(
                [
                    tools["cmake"],
                    "--build",
                    build_dir,
                    "--target",
                    install_target,
                    "--parallel",
                    str(options.jobs),
                ],
                env=build_env,
            )

        managed_runtime_build_dir = _build_macos_managed_libcxx(
            tools["cmake"],
            tools["ninja"],
            llvm_source,
            build_dir,
            install_prefix,
            options,
            build_env,
        )
        created_aliases = []
        if _tool_selection_requires_libxml2(options, sys.platform):
            created_aliases.extend(ensure_windows_mt_alias(install_prefix))
        created_aliases.extend(ensure_versioned_binaries(install_prefix, major))
        managed_architectures = (
            MACOS_MANAGED_LIBCXX_ARCHITECTURES
            if sys.platform == "darwin" and options.cxx_standard_library == MANAGED_LIBCXX_STANDARD_LIBRARY
            else ()
        )
        standard_library, standard_library_files = configure_standard_library(
            install_prefix,
            major,
            build_env,
            options.cxx_standard_library,
            architectures=managed_architectures,
        )
        manager_files = [*created_aliases, *standard_library_files]
        if options.verify:
            _verify_install(
                install_prefix,
                major,
                build_env,
                run_executables=options.targets.strip().lower() in {"all", "host", "native"},
                cxx_architectures=managed_architectures,
                required_tools=_verification_tools(options, sys.platform),
            )

        metadata = {
            "schema_version": 5,
            "source": resolved_revision.to_json(),
            "major": major,
            "configuration_hash": config_hash,
            "build_type": options.build_type,
            "projects": list(options.projects),
            "runtimes": list(_effective_runtimes(options)),
            "tools": list(_normalized_tools(options.tools)),
            "cxx_standard_library": standard_library,
            "targets": options.targets,
            "repository": options.repository_url,
            "host_toolchain": options.host_toolchain.to_json(),
            "versioned_aliases": [str(path.relative_to(install_prefix)) for path in created_aliases],
        }
        if standard_library.get("kind") == MANAGED_LIBCXX_STANDARD_LIBRARY:
            metadata["managed_cxx_standard_library"] = managed_libcxx_capability(standard_library)
        install_build_dirs = (build_dir,) + (
            (managed_runtime_build_dir,) if managed_runtime_build_dir is not None else ()
        )
        metadata["installed_files"] = _installed_files(install_build_dirs, install_prefix, manager_files)
        write_json(install_prefix / ".llvm-manager.json", metadata)
        return install_prefix
