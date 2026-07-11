from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .builder import BuildOptions, build_and_install
from .config import ManagerPaths
from .dependencies import (
    check_build_dependencies,
    offer_dependency_install,
    print_dependency_report,
    require_build_dependencies,
)
from .discovery import print_installs, scan_installs
from .models import InstallInfo
from .repository import (
    DEFAULT_REPOSITORY_URL,
    RevisionKind,
    SourceRevision,
    fetch_release_tags,
    fetch_remote_branches,
)
from .switcher import switch_install
from .toolchains import (
    HostToolchain,
    prompt_for_host_toolchain,
    select_host_toolchain,
)
from .util import LLVMManagerError
from .versioning import LLVMVersion, latest_per_major, normalize_tag

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace(";", ",").split(",") if item.strip())


def _paths(arguments: argparse.Namespace) -> ManagerPaths:
    return ManagerPaths.create(Path(arguments.root), Path(arguments.home) if arguments.home else None)


def _find_install(installs: list[InstallInfo], selector: str) -> InstallInfo:
    selector = selector.strip()
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(installs):
            return installs[index - 1]

    candidate_path = Path(selector).expanduser()
    if candidate_path.exists():
        resolved = candidate_path.resolve()
        if resolved.name.lower() == "bin":
            resolved = resolved.parent
        for install in installs:
            if install.prefix == resolved:
                return install
        clang = resolved / "bin" / ("clang.exe" if os.name == "nt" else "clang")
        if clang.is_file():
            return InstallInfo(resolved, clang, None, managed=False)

    normalized = selector.removeprefix("llvmorg-")
    matches = [
        install
        for install in installs
        if install.version
        and (
            install.version.display == normalized
            or str(install.version.major) == normalized
            or install.tag == selector
        )
    ]
    if matches:
        return max(matches, key=lambda item: item.version or LLVMVersion(0, 0, 0))
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
        selection = input("LLVM version: ").strip()
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
):
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
        selection = input("LLVM branch: ").strip()
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
        selection = input("Select a source type [1]: ").strip() or "1"
        if selection == "1":
            return SourceRevision(RevisionKind.TAG, _select_tag(fetch_release_tags(repository_url)))
        if selection == "2":
            return SourceRevision(RevisionKind.BRANCH, _select_branch(fetch_remote_branches(repository_url)))
        if selection == "3":
            return SourceRevision(RevisionKind.COMMIT, input("Commit ID: ").strip())
        print("Choose 1, 2, or 3.")


def _default_install(paths: ManagerPaths, revision: SourceRevision) -> Path:
    return paths.install_root / revision.directory_name


def _interactive_build(paths: ManagerPaths, repository_url: str) -> Path:
    host_toolchain = _choose_host_toolchain(None, prompt_install=True)
    revision = _select_revision(repository_url)
    default = _default_install(paths, revision)
    response = input(f"Install directory [{default}]: ").strip()
    install_prefix = Path(response).expanduser() if response else default
    target_response = input("LLVM targets to build [Native; enter 'all' for every backend]: ").strip()
    targets = target_response or "Native"
    jobs_response = input(f"Parallel build jobs [{max(1, os.cpu_count() or 1)}]: ").strip()
    jobs = int(jobs_response) if jobs_response else max(1, os.cpu_count() or 1)
    return build_and_install(
        paths,
        BuildOptions(
            revision=revision,
            install_prefix=install_prefix,
            host_toolchain=host_toolchain,
            targets=targets,
            jobs=jobs,
            repository_url=repository_url,
        ),
    )


def _menu(paths: ManagerPaths, repository_url: str) -> int:
    while True:
        print("\nLLVM Manager")
        print("1) Check for existing installs")
        print("2) Display the installed versions")
        print("3) Switch installed version")
        print("4) Build & install an LLVM source revision")
        print("5) Exit")
        choice = input("Select an option: ").strip()

        if choice == "1":
            installs = scan_installs(paths)
            print(f"Found {len(installs)} LLVM/Clang installation(s).")
        elif choice == "2":
            print_installs(scan_installs(paths))
        elif choice == "3":
            installs = scan_installs(paths)
            print_installs(installs)
            if not installs:
                continue
            selected = _find_install(installs, input("Version, list number, or install path: "))
            profile = switch_install(paths, selected)
            print(f"Selected {selected.label}")
            if profile:
                print(f"Updated {profile}. Open a new shell or run: source {profile}")
            else:
                print("Updated the user environment. Open a new terminal to use it.")
        elif choice == "4":
            prefix = _interactive_build(paths, repository_url)
            print(f"Installed LLVM at {prefix}")
        elif choice == "5":
            return 0
        else:
            print("Choose 1, 2, 3, 4, or 5.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llvm-manager",
        description="Download, build, install, discover, and switch versioned LLVM/Clang toolchains.",
    )
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="Manager data root (default: script directory)")
    parser.add_argument("--home", help=argparse.SUPPRESS)
    parser.add_argument("--repo-url", default=DEFAULT_REPOSITORY_URL, help="LLVM Git repository URL")
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser("menu", help="Open the interactive menu")

    scan = subcommands.add_parser("scan", help="Scan for managed and external LLVM installs")
    scan.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    listing = subcommands.add_parser("list", help="Display installed LLVM versions")
    listing.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    find_tools = subcommands.add_parser("find-tools", help="Check LLVM build dependencies and find host compilers")
    find_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    find_tools.add_argument(
        "--install-missing",
        action="store_true",
        help="Run the suggested dependency installer without asking for confirmation",
    )
    find_tools.add_argument(
        "--no-install-prompt",
        action="store_true",
        help="Report missing dependencies without offering to install them",
    )

    tags = subcommands.add_parser("tags", help="Fetch LLVM release tags")
    tags.add_argument("--all", action="store_true", help="Show every release instead of latest per major")
    tags.add_argument("--include-prerelease", action="store_true", help="Include release candidates")

    switch = subcommands.add_parser("switch", help="Select an installed LLVM version")
    switch.add_argument("selector", nargs="?", help="List number, major/full version, tag, or install prefix")
    switch.add_argument("--shell", help="Shell executable/name used to select the profile")
    switch.add_argument("--profile", type=Path, help="Explicit shell profile to update")

    build = subcommands.add_parser("build", help="Fetch, build, and install LLVM from a tag, branch, or commit")
    build.add_argument("tag", nargs="?", help="LLVM release tag or version, for example llvmorg-22.1.8 or 22.1.8")
    revision = build.add_mutually_exclusive_group()
    revision.add_argument("--branch", help="Build the current commit of a remote branch, for example main or release/22.x")
    revision.add_argument("--commit", help="Build an exact 7- to 40-character hexadecimal commit ID")
    build.add_argument("--install-dir", type=Path, help="Versioned installation prefix")
    build.add_argument(
        "--toolchain",
        help="Host compiler list number, ID, family, name, or compiler path; prompts when omitted",
    )
    build.add_argument("--build-type", default="Release", choices=["Debug", "Release", "RelWithDebInfo", "MinSizeRel"])
    build.add_argument("--projects", default="clang,clang-tools-extra,lld", help="Comma-separated LLVM projects")
    build.add_argument("--runtimes", default="compiler-rt", help="Comma-separated LLVM runtimes; empty disables")
    build.add_argument("--targets", default="Native", help="LLVM targets, semicolon-separated, or 'all'")
    build.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    build.add_argument("--no-verify", action="store_true", help="Skip clang version and compile checks")
    build.add_argument(
        "--install-missing",
        action="store_true",
        help="Run the suggested dependency installer without asking for confirmation",
    )
    build.add_argument(
        "--no-install-prompt",
        action="store_true",
        help="Do not offer to install missing build dependencies",
    )
    build.add_argument("--switch", action="store_true", help="Switch to the new install after a successful build")
    build.add_argument("--shell", help="Shell executable/name when used with --switch")
    build.add_argument("--profile", type=Path, help="Explicit shell profile when used with --switch")
    return parser


def _json_installs(installs: list[InstallInfo]) -> None:
    print(json.dumps([install.to_json() for install in installs], indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    paths = _paths(arguments)
    command = arguments.command or "menu"

    try:
        if command == "menu":
            return _menu(paths, arguments.repo_url)
        if command in {"scan", "list"}:
            installs = scan_installs(paths)
            if arguments.json:
                _json_installs(installs)
            elif command == "scan":
                print(f"Found {len(installs)} LLVM/Clang installation(s).")
                print_installs(installs)
            else:
                print_installs(installs)
            return 0
        if command == "find-tools":
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
        if command == "tags":
            versions = fetch_release_tags(arguments.repo_url)
            if not arguments.include_prerelease:
                versions = [version for version in versions if version.stable]
            selected = versions if arguments.all else latest_per_major(versions)
            for version in selected:
                print(version.tag)
            return 0
        if command == "switch":
            installs = scan_installs(paths)
            if not installs:
                raise LLVMManagerError("No LLVM installations were found")
            selector = arguments.selector
            if selector is None:
                print_installs(installs)
                selector = input("Version, list number, or install path: ")
            selected = _find_install(installs, selector)
            profile = switch_install(paths, selected, shell=arguments.shell, profile=arguments.profile)
            print(f"Selected {selected.label}")
            if profile:
                print(f"Updated {profile}. Open a new shell or run: source {profile}")
            else:
                print("Updated the user environment. Open a new terminal to use it.")
            return 0
        if command == "build":
            host_toolchain = _choose_host_toolchain(
                arguments.toolchain,
                install_missing=arguments.install_missing,
                prompt_install=not arguments.no_install_prompt and sys.stdin.isatty(),
            )
            if arguments.tag and (arguments.branch or arguments.commit):
                raise LLVMManagerError("The positional release tag cannot be combined with --branch or --commit")
            if arguments.branch:
                source_revision = SourceRevision(RevisionKind.BRANCH, arguments.branch)
            elif arguments.commit:
                source_revision = SourceRevision(RevisionKind.COMMIT, arguments.commit)
            elif arguments.tag:
                source_revision = SourceRevision(RevisionKind.TAG, normalize_tag(arguments.tag))
            else:
                source_revision = _select_revision(arguments.repo_url)
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
                    targets=arguments.targets,
                    jobs=arguments.jobs,
                    repository_url=arguments.repo_url,
                    verify=not arguments.no_verify,
                ),
            )
            print(f"Installed LLVM at {prefix}")
            if arguments.switch:
                installs = scan_installs(paths)
                selected = next(install for install in installs if install.prefix == prefix.resolve())
                profile = switch_install(paths, selected, shell=arguments.shell, profile=arguments.profile)
                print(f"Selected the new install. Updated {profile}" if profile else "Selected the new install.")
            return 0
    except (LLVMManagerError, ValueError, OSError) as error:
        print(f"llvm-manager: error: {error}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {command}")
    return 2
