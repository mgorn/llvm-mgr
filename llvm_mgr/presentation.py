from __future__ import annotations

from .dependencies import DependencyReport
from .models import InstallInfo
from .toolchains import HostToolchain


def print_installs(installs: list[InstallInfo]) -> None:
    if not installs:
        print("No LLVM/Clang installations found.")
        return
    for index, install in enumerate(installs, start=1):
        marker = "*" if install.active else " "
        print(f"{marker} {index:>2}) {install.label}")


def print_host_toolchains(toolchains: list[HostToolchain]) -> None:
    if not toolchains:
        print("No usable host C/C++ compiler toolchains were found.")
        return
    print("Available host compiler toolchains:")
    for index, toolchain in enumerate(toolchains, start=1):
        print(f"  {index:>2}) {toolchain.label}")
        if toolchain.cxx != toolchain.cc:
            print(f"      C++: {toolchain.cxx}")
        print(f"      ID: {toolchain.identifier}; source: {toolchain.source}")


def print_dependency_report(report: DependencyReport, *, show_toolchains: bool = True) -> None:
    print("LLVM build dependency check:")
    for program in report.programs:
        if program.found:
            version = f" {program.version}" if program.version else ""
            print(f"  [OK]      {program.name}{version}: {program.path}")
        else:
            print(f"  [MISSING] {program.name}: needed to {program.purpose}")

    if report.compiler_available:
        count = len(report.toolchains)
        suffix = "" if count == 1 else "s"
        print(f"  [OK]      Host compiler: {count} usable C/C++ toolchain{suffix} found")
    else:
        print("  [MISSING] Host compiler: GCC, Clang/AppleClang, ClangCL, or MSVC is required")

    if report.ready:
        print("All required LLVM build dependencies are available.")
    else:
        print("\nRequired dependencies are missing.")
        print(report.install_hint)

    if show_toolchains and report.toolchains:
        print()
        print_host_toolchains(list(report.toolchains))
