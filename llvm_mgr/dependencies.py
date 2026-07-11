from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from .toolchains import HostToolchain, discover_host_toolchains
from .util import LLVMManagerError, command_text, extract_numeric_version, probe_text, run, shutil_which

_DEPENDENCY_ORDER = ("git", "cmake", "ninja", "compiler")


@dataclass(frozen=True)
class ProgramDependency:
    identifier: str
    name: str
    purpose: str
    executable: str
    path: Path | None
    version: str | None = None

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
    available: bool = True

    @property
    def display(self) -> str:
        return " && ".join(command_text(command) for command in self.commands)

    def to_json(self) -> dict[str, object]:
        return {
            "manager": self.manager,
            "commands": [list(command) for command in self.commands],
            "display": self.display,
            "requires_elevation": self.requires_elevation,
            "available": self.available,
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
        return tuple(program for program in self.programs if not program.found)

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


@dataclass(frozen=True)
class _LinuxManager:
    names: tuple[str, ...]
    distributions: frozenset[str]
    packages: Mapping[str, tuple[str, ...]]
    install_arguments: tuple[str, ...]
    update_arguments: tuple[str, ...] = ()

    def commands(self, executable: str, selected: tuple[str, ...], elevation: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
        commands: list[tuple[str, ...]] = []
        if self.update_arguments:
            commands.append((*elevation, executable, *self.update_arguments))
        commands.append((*elevation, executable, *self.install_arguments, *selected))
        return tuple(commands)


_LINUX_MANAGERS = (
    _LinuxManager(
        ("apt-get",),
        frozenset({"debian", "ubuntu", "linuxmint", "pop"}),
        {"git": ("git",), "cmake": ("cmake",), "ninja": ("ninja-build",), "compiler": ("build-essential",)},
        ("install", "-y"),
        ("update",),
    ),
    _LinuxManager(
        ("dnf",),
        frozenset({"fedora", "rhel", "centos", "rocky", "almalinux"}),
        {"git": ("git",), "cmake": ("cmake",), "ninja": ("ninja-build",), "compiler": ("gcc", "gcc-c++")},
        ("install", "-y"),
    ),
    _LinuxManager(
        ("pacman",),
        frozenset({"arch", "manjaro", "garuda"}),
        {"git": ("git",), "cmake": ("cmake",), "ninja": ("ninja",), "compiler": ("base-devel",)},
        ("-S", "--needed", "--noconfirm"),
    ),
    _LinuxManager(
        ("zypper",),
        frozenset({"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles"}),
        {"git": ("git",), "cmake": ("cmake",), "ninja": ("ninja",), "compiler": ("gcc", "gcc-c++")},
        ("--non-interactive", "install"),
    ),
    _LinuxManager(
        ("apk",),
        frozenset({"alpine"}),
        {"git": ("git",), "cmake": ("cmake",), "ninja": ("ninja",), "compiler": ("build-base",)},
        ("add",),
    ),
)


def _probe_version(path: Path, arguments: tuple[str, ...] = ("--version",)) -> str | None:
    output = probe_text([path, *arguments])
    return extract_numeric_version(output)


def _find_program(
    identifier: str,
    name: str,
    purpose: str,
    executable: str,
    search_path: str,
) -> ProgramDependency:
    found = shutil_which(executable, search_path)
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


def _elevation(search_path: str) -> tuple[tuple[str, ...], bool, bool]:
    if os.name == "nt":
        return (), False, True
    try:
        if os.geteuid() == 0:
            return (), False, True
    except AttributeError:
        return (), False, True
    sudo = shutil_which("sudo", search_path)
    return ((sudo or "sudo",), True, sudo is not None)


def _select_packages(manager: _LinuxManager, missing: set[str]) -> tuple[str, ...]:
    return tuple(
        package
        for key in _DEPENDENCY_ORDER
        if key in missing
        for package in manager.packages[key]
    )


def _linux_install_plan(missing: set[str], search_path: str) -> DependencyInstallPlan | None:
    distribution = _linux_distribution()
    preferred = [manager for manager in _LINUX_MANAGERS if distribution in manager.distributions]
    managers = [*preferred, *(manager for manager in _LINUX_MANAGERS if manager not in preferred)]

    selected_manager: _LinuxManager | None = None
    executable: str | None = None
    for manager in managers:
        for name in manager.names:
            found = shutil_which(name, search_path)
            if found:
                selected_manager = manager
                executable = found
                break
        if selected_manager:
            break
    if selected_manager is None or executable is None:
        return None

    elevation, required, available = _elevation(search_path)
    note = ""
    if required and not available:
        note = "Automatic installation requires sudo, but sudo was not found on the selected PATH."
    return DependencyInstallPlan(
        manager=selected_manager.names[0],
        commands=selected_manager.commands(executable, _select_packages(selected_manager, missing), elevation),
        requires_elevation=required,
        available=available,
        note=note,
    )


def _macos_install_plan(missing: set[str], search_path: str) -> DependencyInstallPlan | None:
    brew = shutil_which("brew", search_path)
    if brew:
        packages = {"git": "git", "cmake": "cmake", "ninja": "ninja", "compiler": "llvm"}
        selected = tuple(packages[key] for key in _DEPENDENCY_ORDER if key in missing)
        return DependencyInstallPlan(
            manager="homebrew",
            commands=((brew, "install", *selected),),
            requires_elevation=False,
            note="Homebrew LLVM may need its bin directory added to PATH after installation.",
        )

    xcode_select = shutil_which("xcode-select", search_path)
    if "compiler" in missing and xcode_select:
        return DependencyInstallPlan(
            manager="xcode-select",
            commands=((xcode_select, "--install"),),
            requires_elevation=False,
            note=(
                "The Apple developer-tools installer opens interactively. After it finishes, install any still-missing "
                "Git/CMake/Ninja tools and rerun the dependency check."
            ),
        )
    return None


def _windows_install_plan(missing: set[str], search_path: str) -> DependencyInstallPlan | None:
    winget = shutil_which("winget", search_path)
    if winget:
        commands: list[tuple[str, ...]] = []
        package_ids = {"git": "Git.Git", "cmake": "Kitware.CMake", "ninja": "Ninja-build.Ninja"}
        for key in ("git", "cmake", "ninja"):
            if key in missing:
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

    choco = shutil_which("choco", search_path)
    if choco:
        packages = {
            "git": ("git",),
            "cmake": ("cmake",),
            "ninja": ("ninja",),
            "compiler": ("visualstudio2022buildtools", "visualstudio2022-workload-vctools"),
        }
        selected = tuple(package for key in _DEPENDENCY_ORDER if key in missing for package in packages[key])
        return DependencyInstallPlan(
            manager="chocolatey",
            commands=((choco, "install", "-y", *selected),),
            requires_elevation=True,
            note="Run llvm-manager from an elevated terminal and open a new terminal afterward.",
        )
    return None


def _installation_plan(missing: set[str], search_path: str) -> DependencyInstallPlan | None:
    if not missing:
        return None
    if os.name == "nt":
        return _windows_install_plan(missing, search_path)
    if sys.platform == "darwin":
        return _macos_install_plan(missing, search_path)
    return _linux_install_plan(missing, search_path)


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
    install_plan = _installation_plan(missing_keys, path_value)
    return DependencyReport(
        programs=programs,
        toolchains=toolchains,
        install_hint=_installation_hint(missing_names, install_plan) if missing_names else "",
        install_plan=install_plan,
    )


def run_dependency_install(plan: DependencyInstallPlan) -> None:
    if not plan.commands:
        raise LLVMManagerError("No dependency installation command is available for this platform")
    if not plan.available:
        raise LLVMManagerError(plan.note or "The dependency installer cannot be run automatically")
    for command in plan.commands:
        run(command)


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

    if not plan.available:
        return report
    if not assume_yes:
        if not prompt:
            return report
        reader = input_fn or input
        try:
            response = reader("Run the dependency installation command now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt) as error:
            raise LLVMManagerError("Dependency installation was cancelled") from error
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
        "Run `llvm-manager find-tools` after installing them to verify the setup."
    )
