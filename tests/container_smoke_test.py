#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from llvm_mgr.config import ManagerPaths
from llvm_mgr.discovery import scan_installs
from llvm_mgr.standard_library import switch_standard_library

CLI = PROJECT / "llvm_manager.py"


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        completed.check_returncode()
    return completed


def git(cwd: Path, *arguments: str) -> None:
    run(["git", *arguments], cwd=cwd)


def make_origin(path: Path) -> None:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "container-test@example.com")
    git(path, "config", "user.name", "Container Test")
    llvm = path / "llvm"
    llvm.mkdir()
    (llvm / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "include(${CMAKE_CURRENT_SOURCE_DIR}/../cmake/Modules/LLVMVersion.cmake)\n"
        "project(fake_llvm)\n",
        encoding="utf-8",
    )
    version_module = path / "cmake" / "Modules" / "LLVMVersion.cmake"
    version_module.parent.mkdir(parents=True)
    version_module.write_text(
        "if(NOT DEFINED LLVM_VERSION_MAJOR)\n"
        "  set(LLVM_VERSION_MAJOR 22)\n"
        "endif()\n",
        encoding="utf-8",
    )
    git(path, "add", ".")
    git(path, "commit", "-m", "fake LLVM source")
    git(path, "tag", "llvmorg-22.1.8")
    git(path, "branch", "test-branch")


def make_fake_host_compiler(path: Path) -> Path:
    clang = path / "clang"
    clang.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'clang version 18.1.8 (llvm-manager host simulation)'\n"
        "output=''\n"
        "previous=''\n"
        "for argument in \"$@\"; do\n"
        "    if [ \"$previous\" = '-o' ]; then output=$argument; fi\n"
        "    previous=$argument\n"
        "done\n"
        "if [ -n \"$output\" ]; then\n"
        "    printf '#!/bin/sh\\nexit 0\\n' > \"$output\"\n"
        "    chmod +x \"$output\"\n"
        "fi\n",
        encoding="utf-8",
    )
    clang.chmod(0o755)
    (path / "clang++").symlink_to("clang")
    return clang


def make_fake_cmake(path: Path) -> None:
    script = path / "cmake"
    script.write_text(
        """#!/usr/bin/env python3
import os
import pathlib
import sys

args = sys.argv[1:]
if '--build' not in args:
    build = pathlib.Path(args[args.index('-B') + 1])
    prefix_arg = next(arg for arg in args if arg.startswith('-DCMAKE_INSTALL_PREFIX='))
    prefix = prefix_arg.split('=', 1)[1]
    build.mkdir(parents=True, exist_ok=True)
    (build / 'fake-prefix.txt').write_text(prefix, encoding='utf-8')
    (build / 'configure-args.txt').write_text('\\n'.join(args), encoding='utf-8')
    raise SystemExit(0)

build = pathlib.Path(args[args.index('--build') + 1])
prefix = pathlib.Path((build / 'fake-prefix.txt').read_text(encoding='utf-8'))
bin_dir = prefix / 'bin'
bin_dir.mkdir(parents=True, exist_ok=True)
clang = bin_dir / 'clang'
clang.write_text('''#!/usr/bin/env python3
import pathlib
import sys
if '--version' in sys.argv:
    print('clang version 22.1.8 (llvm-manager container simulation)')
    raise SystemExit(0)
if '--print-target-triple' in sys.argv or '-dumpmachine' in sys.argv:
    print('x86_64-unknown-linux-gnu')
    raise SystemExit(0)
if '-o' in sys.argv:
    output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
    output.write_text(chr(35) + '!/bin/sh' + chr(10) + 'exit 0' + chr(10), encoding='utf-8')
    output.chmod(0o755)
    raise SystemExit(0)
raise SystemExit(0)
''', encoding='utf-8')
clang.chmod(0o755)
for name in ('clang++', 'clang-cpp', 'llvm-config', 'ld.lld'):
    target = bin_dir / name
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to('clang')

configure_args = (build / 'configure-args.txt').read_text(encoding='utf-8')
if 'libcxx' in configure_args:
    (prefix / 'include' / 'c++' / 'v1').mkdir(parents=True, exist_ok=True)
    library_dir = prefix / 'lib' / 'x86_64-unknown-linux-gnu'
    library_dir.mkdir(parents=True, exist_ok=True)
    (library_dir / 'libc++.so').write_text('', encoding='utf-8')
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="llvm-manager-container-") as temporary:
        base = Path(temporary)
        origin = base / "origin"
        manager_root = base / "manager"
        home = base / "home"
        fake_bin = base / "fake-bin"
        fake_bin.mkdir()
        make_origin(origin)
        host_compiler = make_fake_host_compiler(fake_bin)
        make_fake_cmake(fake_bin)
        for name, target in (
            ("git", shutil.which("git")),
            ("ninja", shutil.which("ninja")),
            ("chmod", shutil.which("chmod")),
            ("python3", sys.executable),
        ):
            if not target:
                raise RuntimeError(f"Smoke-test prerequisite not found: {name}")
            (fake_bin / name).symlink_to(target)

        env = os.environ.copy()
        env["PATH"] = str(fake_bin)

        finder = run(
            [sys.executable, str(CLI), "find-tools", "--json"],
            env=env,
        )
        dependency_report = json.loads(finder.stdout)
        assert dependency_report["ready"] is True
        assert dependency_report["missing"] == []
        found_toolchains = dependency_report["toolchains"]
        assert len(found_toolchains) == 1
        assert found_toolchains[0]["cc"] == str(host_compiler)
        assert found_toolchains[0]["cxx"] == str(fake_bin / "clang++")
        assert all(program["found"] for program in dependency_report["programs"])

        build = run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(manager_root),
                "--home",
                str(home),
                "--repo-url",
                str(origin),
                "build",
                "22.1.8",
                "--jobs",
                "2",
                "--stdlib",
                "managed-libc++",
                "--switch",
                "--shell",
                "/bin/bash",
            ],
            env=env,
            input_text="\n",
        )
        print(build.stdout)

        install = manager_root / "install" / "llvmorg-22.1.8"
        assert (install / "bin" / "clang-22").exists()
        assert (install / "bin" / "clang++-22").exists()
        metadata = json.loads((install / ".llvm-manager.json").read_text(encoding="utf-8"))
        assert metadata["source"]["kind"] == "tag"
        assert metadata["source"]["value"] == "llvmorg-22.1.8"
        assert len(metadata["source"]["commit"]) == 40
        assert metadata["host_toolchain"]["family"] == "clang"
        assert metadata["host_toolchain"]["cc"] == str(host_compiler)
        standard_library = metadata["cxx_standard_library"]
        assert standard_library["kind"] == "managed-libc++"
        assert metadata["managed_cxx_standard_library"]["kind"] == "managed-libc++"
        assert standard_library["target"] == "x86_64-unknown-linux-gnu"
        assert standard_library["headers"] == "include/c++/v1"
        assert standard_library["libraries"] == "lib/x86_64-unknown-linux-gnu"
        config = install / standard_library["config"]
        assert config.is_file()
        config_text = config.read_text(encoding="utf-8")
        assert "-stdlib=libc++" in config_text
        assert "-nostdinc++" in config_text
        assert "-nostdlib++" not in config_text
        assert "-Wl,-rpath," in config_text
        build_dirs = list((manager_root / "build").iterdir())
        assert len(build_dirs) == 1
        configure_args = (build_dirs[0] / "configure-args.txt").read_text(encoding="utf-8")
        assert f"-DCMAKE_C_COMPILER={host_compiler}" in configure_args
        assert f"-DCMAKE_CXX_COMPILER={fake_bin / 'clang++'}" in configure_args
        assert "-DLLVM_ENABLE_RUNTIMES=compiler-rt;libcxx;libcxxabi;libunwind" in configure_args
        assert "-DLIBCXXABI_USE_LLVM_UNWINDER=ON" in configure_args
        assert (manager_root / "current").resolve() == install.resolve()
        assert "# >>> llvm-manager >>>" in (home / ".bashrc").read_text(encoding="utf-8")

        listing = run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(manager_root),
                "--home",
                str(home),
                "list",
                "--json",
            ],
            env=env,
        )
        installs = json.loads(listing.stdout)
        assert any(
            item["version"] == "22.1.8"
            and item["active"]
            and item["cxx_standard_library"] == "managed-libc++"
            and item["cxx_standard_library_version"] == "22.1.8"
            and item["managed_libcxx_available"]
            for item in installs
        )

        paths = ManagerPaths.create(manager_root, home)
        managed_installs = scan_installs(paths, include_external=False)
        active = next(item for item in managed_installs if item.active)
        switch_standard_library(paths, active, None, env=env)
        switched_metadata = json.loads((install / ".llvm-manager.json").read_text(encoding="utf-8"))
        assert switched_metadata["cxx_standard_library"] == {"kind": "system"}
        assert switched_metadata["managed_cxx_standard_library"]["kind"] == "managed-libc++"
        assert not config.exists()

        managed_installs = scan_installs(paths, include_external=False)
        active = next(item for item in managed_installs if item.active)
        provider = next(item for item in managed_installs if item.managed_libcxx_available)
        switch_standard_library(paths, active, provider, env=env)
        switched_metadata = json.loads((install / ".llvm-manager.json").read_text(encoding="utf-8"))
        assert switched_metadata["cxx_standard_library"]["provider_version"] == "22.1.8"
        assert (install / switched_metadata["cxx_standard_library"]["config"]).is_file()

        branch_build = run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(manager_root),
                "--home",
                str(home),
                "--repo-url",
                str(origin),
                "build",
                "--branch",
                "test-branch",
                "--toolchain",
                "clang-18-1-8",
                "--jobs",
                "2",
            ],
            env=env,
        )
        print(branch_build.stdout)
        branch_install = manager_root / "install" / "branch-test-branch"
        assert (branch_install / "bin" / "clang-22").exists()
        branch_metadata = json.loads(
            (branch_install / ".llvm-manager.json").read_text(encoding="utf-8")
        )
        assert branch_metadata["source"]["kind"] == "branch"
        assert branch_metadata["source"]["value"] == "test-branch"
        assert len(branch_metadata["source"]["commit"]) == 40

        print(
            "Container smoke test passed: dependency check -> host-toolchain "
            "discovery/selection -> tag and branch checkout -> configure -> "
            "managed libc++ pairing and reselection -> install -> aliases -> verify -> switch -> discover"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
