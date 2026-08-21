from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from .builder import BuildOptions, build_and_install
from .config import ManagerPaths, default_manager_root
from .dependencies import (
    DependencyReport,
    check_build_dependencies,
    offer_dependency_install,
    require_build_dependencies,
)
from .discovery import inspect_install, scan_installs
from .models import InstallInfo
from .presentation import print_dependency_report, print_installs
from .repository import (
    DEFAULT_REPOSITORY_URL,
    RevisionKind,
    SourceRevision,
    fetch_release_tags,
    fetch_remote_branches,
)
from .switcher import activation_script, switch_install
from .standard_library import (
    CXX_STANDARD_LIBRARY_CHOICES,
    MANAGED_LIBCXX_STANDARD_LIBRARY,
    SYSTEM_CXX_STANDARD_LIBRARY,
    switch_standard_library,
)
from .toolchains import HostToolchain, prompt_for_host_toolchain, select_host_toolchain
from .util import LLVMManagerError, set_command_echo
from .versioning import LLVMVersion, latest_per_major, normalize_tag


def _prompt(message: str, *, default: str | None = None) -> str:
    try:
        value = input(message).strip()
    except EOFError as error:
        raise LLVMManagerError("Interactive input ended unexpectedly; provide the required option on the command line") from error
    except KeyboardInterrupt as error:
        raise LLVMManagerError("Operation cancelled") from error
    return value or (default or "")


def _csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace(";", ",").split(",") if item.strip())


def _paths(arguments: argparse.Namespace, default_root: Path | None = None) -> ManagerPaths:
    root = Path(arguments.root) if arguments.root else default_manager_root(default_root)
    home = Path(arguments.home) if arguments.home else None
    return ManagerPaths.create(root, home)


def _version_matches(installs: list[InstallInfo], selector: str) -> list[InstallInfo]:
    normalized = selector.removeprefix("llvmorg-")
    return [
        install
        for install in installs
        if install.version
        and (
            install.version.display == normalized
            or str(install.version.major) == normalized
            or install.tag == selector
        )
    ]


def _find_install(installs: list[InstallInfo], selector: str) -> InstallInfo:
    selector = selector.strip()
    if not selector:
        raise LLVMManagerError("An install selector is required")

    candidate_path = Path(selector).expanduser()
    if candidate_path.exists():
        inspected = inspect_install(candidate_path)
        if inspected is None:
            raise LLVMManagerError(f"No usable Clang installation was found at {candidate_path}")
        for install in installs:
            if install.prefix == inspected.prefix:
                return install
        return inspected

    matches = _version_matches(installs, selector)
    if matches:
        return max(matches, key=lambda item: item.version or LLVMVersion(0, 0, 0))

    index_text = selector.removeprefix("#") if selector.startswith("#") else selector
    if index_text.isdigit():
        index = int(index_text)
        if 1 <= index <= len(installs):
            return installs[index - 1]
    raise LLVMManagerError(f"No installed LLVM matches {selector!r}")


def _print_tag_choices(versions: list[LLVMVersion]) -> list[LLVMVersion]:
    choices = latest_per_major(versions)
    print("Latest stable release for each LLVM major version:")
    for index, version in enumerate(choices, start=1):
        print(f"  {index:>2}) {version.tag}")
    print("Enter a number, an exact version/tag, or 'all' to list every fetched release tag.")
    return choices


def _select_tag(versions: list[LLVMVersion]) -> str:
    choices = _print_tag_choices(versions)
    available = {version.tag: version for version in versions}
    while True:
        selection = _prompt("LLVM version: ")
        if selection.lower() == "all":
            for version in versions:
                print(version.tag)
            continue
        if selection.isdigit() and 1 <= int(selection) <= len(choices):
            return choices[int(selection) - 1].tag
        normalized = normalize_tag(selection)
        if normalized in available:
            return normalized
        print("That tag was not in the fetched release list. Try again.")


def _prepare_build_dependencies(
    *,
    install_missing: bool = False,
    prompt_install: bool = True,
) -> DependencyReport:
    report = check_build_dependencies()
    print_dependency_report(report)
    if not report.ready:
        updated = offer_dependency_install(
            report,
            assume_yes=install_missing,
            prompt=prompt_install,
        )
        if updated is not report:
            print()
            print_dependency_report(updated)
        report = updated
    require_build_dependencies(report)
    return report


def _choose_host_toolchain(
    selector: str | None,
    *,
    install_missing: bool = False,
    prompt_install: bool = True,
) -> HostToolchain:
    report = _prepare_build_dependencies(
        install_missing=install_missing,
        prompt_install=prompt_install,
    )
    toolchains = list(report.toolchains)
    if selector:
        selected = select_host_toolchain(toolchains, selector)
        print(f"Using host compiler: {selected.label}")
        return selected
    return prompt_for_host_toolchain(toolchains, display=False)


def _select_branch(branches: list[str]) -> str:
    print("Available remote branches:")
    for index, branch in enumerate(branches, start=1):
        print(f"  {index:>2}) {branch}")
    while True:
        selection = _prompt("LLVM branch: ")
        if selection.isdigit() and 1 <= int(selection) <= len(branches):
            return branches[int(selection) - 1]
        if selection in branches:
            return selection
        print("That branch was not in the fetched remote branch list. Try again.")


def _select_revision(repository_url: str) -> SourceRevision:
    while True:
        print("Source revision to build:")
        print("  1) Release tag")
        print("  2) Branch")
        print("  3) Commit")
        selection = _prompt("Select a source type [1]: ", default="1")
        if selection == "1":
            return SourceRevision(RevisionKind.TAG, _select_tag(fetch_release_tags(repository_url)))
        if selection == "2":
            return SourceRevision(RevisionKind.BRANCH, _select_branch(fetch_remote_branches(repository_url)))
        if selection == "3":
            return SourceRevision(RevisionKind.COMMIT, _prompt("Commit ID: "))
        print("Choose 1, 2, or 3.")


def _revision_from_arguments(arguments: argparse.Namespace) -> SourceRevision:
    selected = sum(bool(value) for value in (arguments.tag, arguments.branch, arguments.commit))
    if selected > 1:
        raise LLVMManagerError("Choose exactly one of a release tag, --branch, or --commit")
    if arguments.branch:
        return SourceRevision(RevisionKind.BRANCH, arguments.branch)
    if arguments.commit:
        return SourceRevision(RevisionKind.COMMIT, arguments.commit)
    if arguments.tag:
        return SourceRevision(RevisionKind.TAG, arguments.tag)
    if not sys.stdin.isatty():
        raise LLVMManagerError("A source revision is required in noninteractive mode; provide a tag, --branch, or --commit")
    return _select_revision(arguments.repo_url)


def _default_install(paths: ManagerPaths, revision: SourceRevision) -> Path:
    return paths.install_root / revision.directory_name


def _select_cxx_standard_library() -> str:
    print("C++ standard library:")
    print("  1) Use the platform / Xcode SDK standard library")
    print("  2) Build and pair libc++ from the selected LLVM revision")
    while True:
        selection = _prompt("Select a C++ standard library [1]: ", default="1")
        if selection in {"1", SYSTEM_CXX_STANDARD_LIBRARY}:
            return SYSTEM_CXX_STANDARD_LIBRARY
        if selection in {"2", MANAGED_LIBCXX_STANDARD_LIBRARY, "libc++"}:
            return MANAGED_LIBCXX_STANDARD_LIBRARY
        print("Choose 1 or 2.")


def _select_llvm_tools() -> tuple[str, ...]:
    print("LLVM tools:")
    print("  Enter 'all' to build and install every configured LLVM tool (default).")
    print("  Or enter a comma-separated subset, for example: lld,llvm-ar,llvm-objdump,llvm-mt")
    print("  clang is always included so the result remains a usable managed compiler toolchain.")
    selection = _prompt("LLVM tools to build [all]: ", default="all")
    return _csv_tuple(selection)


def _interactive_build(paths: ManagerPaths, repository_url: str) -> Path:
    revision = _select_revision(repository_url)
    host_toolchain = _choose_host_toolchain(None, prompt_install=True)
    default = _default_install(paths, revision)
    response = _prompt(f"Install directory [{default}]: ")
    install_prefix = Path(response).expanduser() if response else default
    cxx_standard_library = (
        _select_cxx_standard_library() if sys.platform == "darwin" else SYSTEM_CXX_STANDARD_LIBRARY
    )
    tools = _select_llvm_tools()
    targets = _prompt("LLVM targets to build [all; enter 'Native' for host-only]: ", default="all")
    jobs = int(_prompt(f"Parallel build jobs [{max(1, os.cpu_count() or 1)}]: ", default=str(max(1, os.cpu_count() or 1))))
    return build_and_install(
        paths,
        BuildOptions(
            revision=revision,
            install_prefix=install_prefix,
            host_toolchain=host_toolchain,
            tools=tools,
            targets=targets,
            jobs=jobs,
            repository_url=repository_url,
            cxx_standard_library=cxx_standard_library,
        ),
    )


def _active_managed_install(installs: list[InstallInfo]) -> InstallInfo | None:
    if os.name == "nt":
        return None
    return next((install for install in installs if install.active and install.managed), None)


def _system_standard_library_description() -> str:
    return "Xcode SDK standard library" if sys.platform == "darwin" else "platform standard library"


def _standard_library_description(install: InstallInfo) -> str:
    if install.cxx_standard_library != MANAGED_LIBCXX_STANDARD_LIBRARY:
        return _system_standard_library_description()
    version = install.cxx_standard_library_version
    return f"managed libc++ {version}" if version else "managed libc++"


def _interactive_switch_standard_library(
    paths: ManagerPaths,
    compiler: InstallInfo,
    installs: list[InstallInfo],
) -> None:
    providers = [
        install
        for install in installs
        if install.managed and install.managed_libcxx_available
    ]
    print(f"Current compiler: {compiler.label}")
    print(f"Current C++ standard library: {_standard_library_description(compiler)}")
    print("Available C++ standard libraries:")
    print(f"  1) {_system_standard_library_description().capitalize()}")
    for index, provider in enumerate(providers, start=2):
        version = provider.version.display if provider.version else "unknown"
        location = " (same LLVM install)" if provider.prefix == compiler.prefix else ""
        print(f"  {index}) Managed libc++ {version}{location}")

    while True:
        selection = _prompt("Select a C++ standard library: ")
        if selection == "1":
            switch_standard_library(paths, compiler, None)
            system_library = _system_standard_library_description()
            print(f"Selected the {system_library} for LLVM {compiler.version.display}.")
            return
        if selection.isdigit() and 2 <= int(selection) <= len(providers) + 1:
            provider = providers[int(selection) - 2]
            switch_standard_library(paths, compiler, provider)
            version = provider.version.display if provider.version else "unknown"
            print(f"Selected managed libc++ {version} for LLVM {compiler.version.display}.")
            return
        print(f"Choose a number from 1 to {len(providers) + 1}.")


def _menu(paths: ManagerPaths, repository_url: str) -> int:
    if not sys.stdin.isatty():
        raise LLVMManagerError("The interactive menu requires a terminal; choose a subcommand instead")
    while True:
        installs = scan_installs(paths)
        active_managed = _active_managed_install(installs)
        print("\nLLVM Manager")
        print("1) Display installed versions")
        print("2) Switch installed version")
        print("3) Build & install an LLVM source revision")
        if active_managed is not None:
            print("4) Switch C++ standard library")
            print("5) Exit")
        else:
            print("4) Exit")

        choice = _prompt("Select an option: ")
        if choice == "1":
            print(f"Found {len(installs)} LLVM/Clang installation(s).")
            print_installs(installs)
        elif choice == "2":
            print_installs(installs)
            if installs:
                selected = _find_install(installs, _prompt("Version, #list-number, or install path: "))
                profile = switch_install(paths, selected)
                _print_switch_result(paths, selected, profile)
        elif choice == "3":
            print(f"Installed LLVM at {_interactive_build(paths, repository_url)}")
        elif choice == "4" and active_managed is not None:
            _interactive_switch_standard_library(paths, active_managed, installs)
        elif choice == ("5" if active_managed is not None else "4"):
            return 0
        else:
            valid = "1, 2, 3, 4, or 5" if active_managed is not None else "1, 2, 3, or 4"
            print(f"Choose {valid}.")


def build_parser(default_root: Path | None = None) -> argparse.ArgumentParser:
    resolved_default_root = default_manager_root(default_root)
    parser = argparse.ArgumentParser(
        prog="llvm-manager",
        description="Download, build, install, discover, and switch versioned LLVM/Clang toolchains.",
    )
    parser.add_argument("--root", type=Path, help=f"Manager workspace root (default: {resolved_default_root})")
    parser.add_argument("--home", help=argparse.SUPPRESS)
    parser.add_argument("--repo-url", default=DEFAULT_REPOSITORY_URL, help="LLVM Git repository URL")
    parser.add_argument("--quiet", action="store_true", help="Do not echo external commands")
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser("menu", help="Open the interactive menu")
    for name, help_text in (
        ("scan", "Scan for managed and external LLVM installs"),
        ("list", "Display installed LLVM versions"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    find_tools = subcommands.add_parser("find-tools", help="Check LLVM build dependencies and find host compilers")
    find_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    find_tools.add_argument("--install-missing", action="store_true", help="Run the suggested dependency installer without asking")
    find_tools.add_argument("--no-install-prompt", action="store_true", help="Do not offer to install missing dependencies")

    tags = subcommands.add_parser("tags", help="Fetch LLVM release tags")
    tags.add_argument("--all", action="store_true", help="Print every release instead of one per major")
    tags.add_argument("--include-prerelease", action="store_true", help="Include RC and development tags")

    switch = subcommands.add_parser("switch", help="Activate an installed LLVM version")
    switch.add_argument("selector", nargs="?", help="Version, #list-number, or installation prefix")
    switch.add_argument("--shell", help="Shell executable/name")
    switch.add_argument("--profile", type=Path, help="Explicit shell profile to update")

    activate = subcommands.add_parser("activate", help="Show the generated activation script for the selected LLVM")
    activate.add_argument("--shell", help="Shell name (bash, zsh, fish, pwsh, or powershell)")

    build = subcommands.add_parser("build", help="Build and install an LLVM source revision")
    build.add_argument("tag", nargs="?", help="LLVM release version or tag, for example 22.1.8")
    revision = build.add_mutually_exclusive_group()
    revision.add_argument("--branch", help="Build the current commit of a remote branch")
    revision.add_argument("--commit", help="Build an exact 7- to 40-character hexadecimal commit ID")
    build.add_argument("--install-dir", type=Path, help="Versioned installation prefix")
    build.add_argument("--toolchain", help="Host compiler list number, ID, family, name, or compiler path")
    build.add_argument("--build-type", default="Release", choices=["Debug", "Release", "RelWithDebInfo", "MinSizeRel"])
    build.add_argument("--projects", default="clang,clang-tools-extra,lld", help="Comma-separated LLVM projects")
    build.add_argument("--runtimes", default="compiler-rt", help="Comma-separated LLVM runtimes; empty disables")
    build.add_argument(
        "--tools",
        default="all",
        help="LLVM tools to install: 'all' (default) or a comma-separated subset; clang is always included",
    )
    build.add_argument(
        "--stdlib",
        "--cxx-stdlib",
        dest="cxx_standard_library",
        default=SYSTEM_CXX_STANDARD_LIBRARY,
        choices=CXX_STANDARD_LIBRARY_CHOICES,
        help="C++ standard library paired with this install (default: system)",
    )
    build.add_argument("--targets", default="all", help="LLVM targets, semicolon-separated, or 'all' (default: all)")
    build.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    build.add_argument("--clean", action="store_true", help="Clean the build directory and manager-owned install before building")
    build.add_argument("--no-verify", action="store_true", help="Skip installed compiler link and execution checks")
    build.add_argument("--install-missing", action="store_true", help="Run the suggested dependency installer without asking")
    build.add_argument("--no-install-prompt", action="store_true", help="Do not offer to install missing dependencies")
    build.add_argument("--switch", action="store_true", help="Switch to the new install after a successful build")
    build.add_argument("--shell", help="Shell executable/name when used with --switch")
    build.add_argument("--profile", type=Path, help="Explicit shell profile when used with --switch")
    return parser


def _json_installs(installs: list[InstallInfo]) -> None:
    print(json.dumps([install.to_json() for install in installs], indent=2))


def _print_switch_result(paths: ManagerPaths, selected: InstallInfo, profile: Path | None) -> None:
    print(f"Selected {selected.label}")
    if profile:
        print(f"Updated {profile}. Open a new shell or run: source {profile}")
    else:
        print("Updated the user environment. Open a new terminal to use it.")
        print(f"PowerShell activation script: {paths.activation_ps1}")


def _handle_list(arguments: argparse.Namespace, paths: ManagerPaths) -> int:
    installs = scan_installs(paths)
    if arguments.json:
        _json_installs(installs)
    else:
        if arguments.command == "scan":
            print(f"Found {len(installs)} LLVM/Clang installation(s).")
        print_installs(installs)
    return 0


def _handle_find_tools(arguments: argparse.Namespace, paths: ManagerPaths) -> int:
    del paths
    if arguments.json and arguments.install_missing:
        raise LLVMManagerError("--json cannot be combined with --install-missing")
    report = check_build_dependencies()
    if arguments.json:
        print(json.dumps(report.to_json(), indent=2))
        return 0 if report.ready else 1
    print_dependency_report(report)
    if not report.ready:
        updated = offer_dependency_install(
            report,
            assume_yes=arguments.install_missing,
            prompt=not arguments.no_install_prompt and sys.stdin.isatty(),
        )
        if updated is not report:
            print()
            print_dependency_report(updated)
        report = updated
    return 0 if report.ready else 1


def _handle_tags(arguments: argparse.Namespace, paths: ManagerPaths) -> int:
    del paths
    versions = fetch_release_tags(arguments.repo_url)
    if arguments.all:
        selected = versions if arguments.include_prerelease else [version for version in versions if version.stable]
    else:
        selected = latest_per_major(versions, include_prerelease=arguments.include_prerelease)
    for version in selected:
        print(version.tag)
    return 0


def _handle_switch(arguments: argparse.Namespace, paths: ManagerPaths) -> int:
    installs = scan_installs(paths)
    if not installs:
        raise LLVMManagerError("No LLVM installations were found")
    selector = arguments.selector
    if selector is None:
        print_installs(installs)
        selector = _prompt("Version, #list-number, or install path: ")
    selected = _find_install(installs, selector)
    profile = switch_install(paths, selected, shell=arguments.shell, profile=arguments.profile)
    _print_switch_result(paths, selected, profile)
    return 0


def _handle_activate(arguments: argparse.Namespace, paths: ManagerPaths) -> int:
    script = activation_script(paths, arguments.shell)
    if not script.is_file():
        raise LLVMManagerError("No activation script exists yet; switch to an LLVM installation first")
    print(script)
    return 0


def _handle_build(arguments: argparse.Namespace, paths: ManagerPaths) -> int:
    source_revision = _revision_from_arguments(arguments)
    host_toolchain = _choose_host_toolchain(
        arguments.toolchain,
        install_missing=arguments.install_missing,
        prompt_install=not arguments.no_install_prompt and sys.stdin.isatty(),
    )
    install_prefix = arguments.install_dir or _default_install(paths, source_revision)
    prefix = build_and_install(
        paths,
        BuildOptions(
            revision=source_revision,
            install_prefix=install_prefix,
            host_toolchain=host_toolchain,
            build_type=arguments.build_type,
            projects=_csv_tuple(arguments.projects),
            runtimes=_csv_tuple(arguments.runtimes),
            tools=_csv_tuple(arguments.tools),
            targets=arguments.targets,
            jobs=arguments.jobs,
            repository_url=arguments.repo_url,
            cxx_standard_library=arguments.cxx_standard_library,
            verify=not arguments.no_verify,
            clean=arguments.clean,
        ),
    )
    print(f"Installed LLVM at {prefix}")
    if arguments.switch:
        selected = inspect_install(prefix, managed=True)
        if selected is None:
            raise LLVMManagerError(f"The newly installed LLVM could not be inspected: {prefix}")
        profile = switch_install(paths, selected, shell=arguments.shell, profile=arguments.profile)
        _print_switch_result(paths, selected, profile)
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace, ManagerPaths], int]] = {
    "scan": _handle_list,
    "list": _handle_list,
    "find-tools": _handle_find_tools,
    "tags": _handle_tags,
    "switch": _handle_switch,
    "activate": _handle_activate,
    "build": _handle_build,
}


def main(argv: Sequence[str] | None = None, *, default_root: Path | None = None) -> int:
    parser = build_parser(default_root)
    arguments = parser.parse_args(argv)
    set_command_echo(not arguments.quiet)
    paths = _paths(arguments, default_root)
    command = arguments.command or "menu"
    try:
        if command == "menu":
            return _menu(paths, arguments.repo_url)
        return _HANDLERS[command](arguments, paths)
    except (LLVMManagerError, ValueError, OSError, KeyboardInterrupt) as error:
        print(f"llvm-manager: error: {error}", file=sys.stderr)
        return 1
