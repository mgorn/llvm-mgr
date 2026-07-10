from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .toolchains import HostToolchain, discover_host_toolchains, print_host_toolchains
from .util import LLVMManagerError, command_text, run

_VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){0,3})(?!\d)")


@dataclass(frozen=True)
class ProgramDependency:
    identifier: str
    name: str
    purpose: str
    executable: str
    path: Path | None
    version: str | None = None
    required: bool = True

    @property
    def found(self) -> bool:
        return self.path is not None

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["path"] = str(self.path) if self.path else None
        data["found"] = self.found
        return data


@dataclass(frozen=True)
class DependencyInstallPlan:
    manager: str
    commands: tuple[tuple[str, ...], ...]
    requires_elevation: bool
    note: str = ""

    @property
    def display(self) -> str:
        return " && ".join(command_text(command) for command in self.commands)

    def to_json(self) -> dict[str, object]:
        return {
            "manager": self.manager,
            "commands": [list(command) for command in self.commands],
            "display": self.display,
            "requires_elevation": self.requires_elevation,
            "note": self.note or None,
        }


@dataclass(frozen=True)
class DependencyReport:
    programs: tuple[ProgramDependency, ...]
    toolchains: tuple[HostToolchain, ...]
    install_hint: str = ""
    install_plan: DependencyInstallPlan | None = None

    @property
    def missing_programs(self) -> tuple[ProgramDependency, ...]:
        return tuple(program for program in self.programs if program.required and not program.found)

    @property
    def compiler_available(self) -> bool:
        return bool(self.toolchains)

    @property
    def ready(self) -> bool:
        return not self.missing_programs and self.compiler_available

    @property
    def missing_names(self) -> tuple[str, ...]:
        names = [program.name for program in self.missing_programs]
        if not self.compiler_available:
            names.append("usable C/C++ compiler toolchain")
        return tuple(names)

    def to_json(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "missing": list(self.missing_names),
            "programs": [program.to_json() for program in self.programs],
            "toolchains": [toolchain.to_json() for toolchain in self.toolchains],
            "install_hint": self.install_hint if not self.ready else None,
            "install_plan": self.install_plan.to_json() if self.install_plan and not self.ready else None,
        }


def _probe_version(path: Path, arguments: tuple[str, ...] = ("--version",)) -> str | None:
    try:
        completed = subprocess.run(
            [str(path), *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    match = _VERSION_RE.search(output)
    return match.group(1) if match else None


def _find_program(
    identifier: str,
    name: str,
    purpose: str,
    executable: str,
    search_path: str,
) -> ProgramDependency:
    found = shutil.which(executable, path=search_path)
    path = Path(found).absolute() if found else None
    return ProgramDependency(
        identifier=identifier,
        name=name,
        purpose=purpose,
        executable=executable,
        path=path,
        version=_probe_version(path) if path else None,
    )


def _linux_distribution() -> str:
    try:
        values: dict[str, str] = {}
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
        return values.get("ID", "").lower()
    except OSError:
        return ""


def _needs_elevation_prefix() -> tuple[str, ...]:
    if os.name == "nt":
        return ()
    try:
        if os.geteuid() == 0:
            return ()
    except AttributeError:
        pass
    return ("sudo",) if shutil.which("sudo") else ()


def _linux_install_plan(missing: set[str]) -> DependencyInstallPlan | None:
    distribution = _linux_distribution()
    elevated = _needs_elevation_prefix()

    if distribution in {"debian", "ubuntu", "linuxmint", "pop"}:
        packages = {
            "git": "git",
            "cmake": "cmake",
            "ninja": "ninja-build",
            "compiler": "build-essential",
        }
        selected = tuple(packages[key] for key in ("git", "cmake", "ninja", "compiler") if key in missing)
        return DependencyInstallPlan(
            manager="apt",
            commands=(
                (*elevated, "apt-get", "update"),
                (*elevated, "apt-get", "install", "-y", *selected),
            ),
            requires_elevation=True,
        )

    if distribution in {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        packages = {
            "git": ("git",),
            "cmake": ("cmake",),
            "ninja": ("ninja-build",),
            "compiler": ("gcc", "gcc-c++"),
        }
        selected = tuple(package for key in ("git", "cmake", "ninja", "compiler") if key in missing for package in packages[key])
        return DependencyInstallPlan(
            manager="dnf",
            commands=((*elevated, "dnf", "install", "-y", *selected),),
            requires_elevation=True,
        )

    if distribution in {"arch", "manjaro", "garuda"}:
        packages = {
            "git": "git",
            "cmake": "cmake",
            "ninja": "ninja",
            "compiler": "base-devel",
        }
        selected = tuple(packages[key] for key in ("git", "cmake", "ninja", "compiler") if key in missing)
        return DependencyInstallPlan(
            manager="pacman",
            commands=((*elevated, "pacman", "-S", "--needed", "--noconfirm", *selected),),
            requires_elevation=True,
        )

    if distribution in {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles"}:
        packages = {
            "git": ("git",),
            "cmake": ("cmake",),
            "ninja": ("ninja",),
            "compiler": ("gcc", "gcc-c++"),
        }
        selected = tuple(package for key in ("git", "cmake", "ninja", "compiler") if key in missing for package in packages[key])
        return DependencyInstallPlan(
            manager="zypper",
            commands=((*elevated, "zypper", "--non-interactive", "install", *selected),),
            requires_elevation=True,
        )

    if distribution == "alpine":
        packages = {
            "git": "git",
            "cmake": "cmake",
            "ninja": "ninja",
            "compiler": "build-base",
        }
        selected = tuple(packages[key] for key in ("git", "cmake", "ninja", "compiler") if key in missing)
        return DependencyInstallPlan(
            manager="apk",
            commands=((*elevated, "apk", "add", *selected),),
            requires_elevation=True,
        )

    candidates = (
        ("apt-get", {"git": "git", "cmake": "cmake", "ninja": "ninja-build", "compiler": "build-essential"}),
        ("dnf", {"git": "git", "cmake": "cmake", "ninja": "ninja-build", "compiler": "gcc-c++"}),
        ("pacman", {"git": "git", "cmake": "cmake", "ninja": "ninja", "compiler": "base-devel"}),
        ("zypper", {"git": "git", "cmake": "cmake", "ninja": "ninja", "compiler": "gcc-c++"}),
        ("apk", {"git": "git", "cmake": "cmake", "ninja": "ninja", "compiler": "build-base"}),
    )
    for manager, packages in candidates:
        if not shutil.which(manager):
            continue
        selected = tuple(packages[key] for key in ("git", "cmake", "ninja", "compiler") if key in missing)
        if manager == "apt-get":
            commands = (
                (*elevated, manager, "update"),
                (*elevated, manager, "install", "-y", *selected),
            )
        elif manager == "dnf":
            commands = ((*elevated, manager, "install", "-y", *selected),)
        elif manager == "pacman":
            commands = ((*elevated, manager, "-S", "--needed", "--noconfirm", *selected),)
        elif manager == "zypper":
            commands = ((*elevated, manager, "--non-interactive", "install", *selected),)
        else:
            commands = ((*elevated, manager, "add", *selected),)
        return DependencyInstallPlan(manager, commands, requires_elevation=True)
    return None


def _macos_install_plan(missing: set[str]) -> DependencyInstallPlan | None:
    brew = shutil.which("brew")
    if brew:
        packages = {
            "git": "git",
            "cmake": "cmake",
            "ninja": "ninja",
            "compiler": "llvm",
        }
        selected = tuple(packages[key] for key in ("git", "cmake", "ninja", "compiler") if key in missing)
        return DependencyInstallPlan(
            manager="homebrew",
            commands=((brew, "install", *selected),),
            requires_elevation=False,
            note="Homebrew LLVM may need its bin directory added to PATH after installation.",
        )

    if "compiler" in missing:
        return DependencyInstallPlan(
            manager="xcode-select",
            commands=(("xcode-select", "--install"),),
            requires_elevation=True,
            note=(
                "The Apple developer-tools installer opens interactively. After it finishes, install any still-missing "
                "Git/CMake/Ninja tools and rerun the dependency check."
            ),
        )
    return None


def _windows_install_plan(missing: set[str]) -> DependencyInstallPlan | None:
    winget = shutil.which("winget")
    if winget:
        commands: list[tuple[str, ...]] = []
        package_ids = {
            "git": "Git.Git",
            "cmake": "Kitware.CMake",
            "ninja": "Ninja-build.Ninja",
        }
        for key in ("git", "cmake", "ninja"):
            if key not in missing:
                continue
            commands.append(
                (
                    winget,
                    "install",
                    "--id",
                    package_ids[key],
                    "--exact",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                )
            )
        if "compiler" in missing:
            commands.append(
                (
                    winget,
                    "install",
                    "--id",
                    "Microsoft.VisualStudio.2022.BuildTools",
                    "--exact",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                    "--override",
                    "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended",
                )
            )
        return DependencyInstallPlan(
            manager="winget",
            commands=tuple(commands),
            requires_elevation=True,
            note="Windows may display a UAC prompt. Open a new terminal afterward so PATH changes are visible.",
        )

    choco = shutil.which("choco")
    if choco:
        packages = {
            "git": ("git",),
            "cmake": ("cmake",),
            "ninja": ("ninja",),
            "compiler": ("visualstudio2022buildtools", "visualstudio2022-workload-vctools"),
        }
        selected = tuple(package for key in ("git", "cmake", "ninja", "compiler") if key in missing for package in packages[key])
        return DependencyInstallPlan(
            manager="chocolatey",
            commands=((choco, "install", "-y", *selected),),
            requires_elevation=True,
            note="Run llvm-manager from an elevated terminal and open a new terminal afterward.",
        )
    return None


def _installation_plan(missing: set[str]) -> DependencyInstallPlan | None:
    if not missing:
        return None
    if os.name == "nt":
        return _windows_install_plan(missing)
    if sys.platform == "darwin":
        return _macos_install_plan(missing)
    return _linux_install_plan(missing)


def _installation_hint(missing_names: tuple[str, ...], plan: DependencyInstallPlan | None) -> str:
    missing_text = ", ".join(missing_names)
    if plan:
        suffix = f" {plan.note}" if plan.note else ""
        return f"Missing: {missing_text}. Suggested command: `{plan.display}`.{suffix}"

    if os.name == "nt":
        return (
            f"Missing: {missing_text}. Install Git, CMake, and Ninja and ensure they are on PATH. "
            "Install Visual Studio Build Tools with the 'Desktop development with C++' workload, "
            "or install a complete Clang/GCC toolchain."
        )
    if sys.platform == "darwin":
        return (
            f"Missing: {missing_text}. Install Apple's command-line developer tools with "
            "`xcode-select --install`, then install any remaining Git/CMake/Ninja dependencies."
        )
    return f"Missing: {missing_text}. Install Git, CMake, Ninja, and a complete C/C++ compiler toolchain."


def check_build_dependencies(search_path: str | None = None) -> DependencyReport:
    path_value = search_path if search_path is not None else os.environ.get("PATH", "")
    programs = (
        _find_program("git", "Git", "fetch and check out LLVM source", "git", path_value),
        _find_program("cmake", "CMake", "configure the LLVM build", "cmake", path_value),
        _find_program("ninja", "Ninja", "execute the generated LLVM build", "ninja", path_value),
    )
    toolchains = tuple(discover_host_toolchains(search_path))

    missing_keys = {program.identifier for program in programs if not program.found}
    if not toolchains:
        missing_keys.add("compiler")
    missing_names = tuple(
        [program.name for program in programs if not program.found]
        + ([] if toolchains else ["usable C/C++ compiler toolchain"])
    )
    install_plan = _installation_plan(missing_keys)
    return DependencyReport(
        programs=programs,
        toolchains=toolchains,
        install_hint=_installation_hint(missing_names, install_plan) if missing_names else "",
        install_plan=install_plan,
    )


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


def run_dependency_install(plan: DependencyInstallPlan) -> None:
    if not plan.commands:
        raise LLVMManagerError("No dependency installation command is available for this platform")
    for command in plan.commands:
        try:
            run(command)
        except FileNotFoundError as error:
            raise LLVMManagerError(
                f"Could not run the suggested installer because {command[0]!r} was not found"
            ) from error


def offer_dependency_install(
    report: DependencyReport,
    *,
    assume_yes: bool = False,
    prompt: bool = True,
    search_path: str | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> DependencyReport:
    if report.ready or report.install_plan is None:
        return report

    plan = report.install_plan
    print("\nllvm-manager can try to install the missing dependencies with:")
    print(f"  {plan.display}")
    if plan.requires_elevation:
        if os.name == "nt":
            print("This may require administrator privileges and can trigger a Windows UAC prompt.")
        else:
            print("This may require elevated privileges and can prompt for your sudo password.")
    else:
        print("The package manager may still request confirmation during installation.")
    if plan.note:
        print(plan.note)

    if not assume_yes:
        if not prompt:
            return report
        reader = input_fn or input
        response = reader("Run the dependency installation command now? [y/N]: ").strip().lower()
        if response not in {"y", "yes"}:
            print("Dependency installation skipped.")
            return report

    run_dependency_install(plan)
    print("\nDependency installer finished. Rechecking the build environment...")
    return check_build_dependencies(search_path)


def require_build_dependencies(report: DependencyReport) -> None:
    if report.ready:
        return
    missing = ", ".join(report.missing_names)
    raise LLVMManagerError(
        f"LLVM cannot be built until these dependencies are installed: {missing}. "
        "Run `python3 llvm_manager.py find-tools` after installing them to verify the setup."
    )
