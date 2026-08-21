from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from llvm_mgr.aliases import ensure_versioned_binaries
from llvm_mgr.builder import _prepare_build_directory, _verify_install
from llvm_mgr.cli import _find_install, build_parser, main
from llvm_mgr.config import ManagerPaths, default_manager_root
from llvm_mgr.dependencies import DependencyReport
from llvm_mgr.discovery import _candidate_clang, inspect_install, scan_installs
from llvm_mgr.models import InstallInfo
from llvm_mgr.switcher import activation_script, switch_install
from llvm_mgr.toolchains import HostToolchain
from llvm_mgr.util import LLVMManagerError, read_json, replace_managed_block
from llvm_mgr.versioning import LLVMVersion, latest_per_major


def executable(path: Path, text: str = "#!/bin/sh\necho 'clang version 22.1.8'\n") -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@unittest.skipIf(os.name == "nt", "POSIX executable fixtures")
class DiscoveryQualityTests(unittest.TestCase):
    def test_scan_has_no_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing-manager"
            paths = ManagerPaths.create(root, Path(temporary) / "home")
            with patch("llvm_mgr.discovery._common_prefixes", return_value=[]), patch.dict(os.environ, {"PATH": ""}):
                self.assertEqual(scan_installs(paths), [])
            self.assertFalse(root.exists())

    def test_versioned_clang_candidates_sort_numerically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            for major in (9, 18, 22):
                executable(bin_dir / f"clang-{major}")
            self.assertEqual(_candidate_clang(Path(temporary)).name, "clang-22")

    def test_install_inspection_rejects_non_executable_and_failed_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            bin_dir.mkdir()
            clang = bin_dir / "clang"
            clang.write_text("not executable\n", encoding="utf-8")
            self.assertIsNone(inspect_install(prefix))
            clang.chmod(0o755)
            self.assertIsNone(inspect_install(prefix))

    def test_install_inspection_reads_current_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            bin_dir.mkdir()
            executable(bin_dir / "clang-22")
            (prefix / ".llvm-manager.json").write_text(
                '{"source":{"kind":"tag","value":"llvmorg-22.1.8","commit":"abc"},'
                '"cxx_standard_library":{"kind":"managed-libc++"}}',
                encoding="utf-8",
            )
            install = inspect_install(prefix)
            self.assertIsNotNone(install)
            self.assertEqual(install.tag, "llvmorg-22.1.8")
            self.assertTrue(install.managed)
            self.assertEqual(install.cxx_standard_library, "managed-libc++")


class SelectorQualityTests(unittest.TestCase):
    def test_numeric_major_is_preferred_over_legacy_list_index(self) -> None:
        installs = [
            InstallInfo(Path(f"/tmp/llvm-{major}"), Path(f"/tmp/llvm-{major}/bin/clang"), LLVMVersion(major, 0, 0), False)
            for major in range(1, 24)
        ]
        self.assertEqual(_find_install(installs, "22").version.major, 22)
        self.assertEqual(_find_install(installs, "#22"), installs[21])

    def test_latest_per_major_can_include_prereleases(self) -> None:
        versions = [LLVMVersion(23, 0, 0, "rc2"), LLVMVersion(22, 1, 8)]
        self.assertEqual(latest_per_major(versions, include_prerelease=True)[0], versions[0])


class CLIQualityTests(unittest.TestCase):
    def test_build_defaults_to_all_llvm_targets(self) -> None:
        arguments = build_parser().parse_args(["build", "22.1.8"])
        self.assertEqual(arguments.targets, "all")
        self.assertEqual(arguments.tools, "all")

    def test_invalid_revision_is_rejected_before_dependency_probe(self) -> None:
        errors = io.StringIO()
        with patch("llvm_mgr.cli.check_build_dependencies") as dependencies, contextlib.redirect_stderr(errors):
            result = main(["build", "not-a-tag"])
        self.assertEqual(result, 1)
        dependencies.assert_not_called()
        self.assertIn("recognized LLVM release tag", errors.getvalue())

    def test_tags_include_prerelease_without_all(self) -> None:
        output = io.StringIO()
        versions = [LLVMVersion(23, 0, 0, "rc1"), LLVMVersion(22, 1, 8)]
        with patch("llvm_mgr.cli.fetch_release_tags", return_value=versions), contextlib.redirect_stdout(output):
            result = main(["tags", "--include-prerelease"])
        self.assertEqual(result, 0)
        self.assertIn("llvmorg-23.0.0-rc1", output.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_custom_install_can_be_switched_without_rescanning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            prefix = base / "custom"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            executable(bin_dir / "clang-22")
            (bin_dir / "clang++-22").symlink_to("clang-22")
            toolchain = HostToolchain("clang", "Clang", "clang", bin_dir / "clang-22", bin_dir / "clang++-22")
            ready = DependencyReport((), (toolchain,))
            with (
                patch("llvm_mgr.cli.check_build_dependencies", return_value=ready),
                patch("llvm_mgr.cli.build_and_install", return_value=prefix),
                patch("llvm_mgr.cli.switch_install", return_value=base / "profile") as switch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = main(["--root", str(base / "manager"), "build", "22.1.8", "--install-dir", str(prefix), "--toolchain", "clang", "--switch"])
            self.assertEqual(result, 0)
            switch.assert_called_once()
            self.assertEqual(switch.call_args.args[1].prefix, prefix.resolve())


@unittest.skipIf(os.name == "nt", "POSIX alias fixture")
class AliasQualityTests(unittest.TestCase):
    def test_replaces_incorrect_alias_and_skips_unrelated_executables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            bin_dir.mkdir()
            clang = executable(bin_dir / "clang")
            other = executable(bin_dir / "helper-script")
            wrong = executable(bin_dir / "wrong")
            (bin_dir / "clang-22").symlink_to(wrong.name)

            ensure_versioned_binaries(prefix, 22)

            self.assertEqual((bin_dir / "clang-22").resolve(), clang.resolve())
            self.assertFalse((bin_dir / "helper-script-22").exists())
            self.assertTrue(other.exists())


@unittest.skipIf(os.name == "nt", "POSIX switch fixture")
class SwitchQualityTests(unittest.TestCase):
    def test_fish_uses_fish_quoting_and_powershell_script_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="llvm manager 'quote' ") as temporary:
            base = Path(temporary)
            prefix = base / "install"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            clang = executable(bin_dir / "clang")
            install = InstallInfo(prefix, clang, LLVMVersion(22, 1, 8), True)
            paths = ManagerPaths.create(base / "manager", base / "home")

            profile = switch_install(paths, install, shell="fish")

            self.assertEqual(profile, paths.home / ".config" / "fish" / "config.fish")
            self.assertIn("set -gx LLVM_HOME", paths.activation_fish.read_text(encoding="utf-8"))
            self.assertEqual(activation_script(paths, "pwsh"), paths.activation_ps1)

    def test_switch_rolls_back_symlink_and_profile_when_state_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = base / "old"
            new = base / "new"
            for prefix in (old, new):
                (prefix / "bin").mkdir(parents=True)
                executable(prefix / "bin" / "clang")
            paths = ManagerPaths.create(base / "manager", base / "home")
            paths.ensure_root()
            paths.current.symlink_to(old, target_is_directory=True)
            profile = paths.home / ".bashrc"
            profile.parent.mkdir(parents=True)
            profile.write_text("original\n", encoding="utf-8")
            install = InstallInfo(new, new / "bin" / "clang", LLVMVersion(22, 1, 8), True)

            with patch("llvm_mgr.switcher.write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    switch_install(paths, install, shell="bash")

            self.assertEqual(paths.current.resolve(), old.resolve())
            self.assertEqual(profile.read_text(encoding="utf-8"), "original\n")


class UtilityQualityTests(unittest.TestCase):
    def test_malformed_and_duplicate_managed_blocks_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile"
            path.write_text(
                "before\n# >>> llvm-manager >>>\nstale\n# <<< llvm-manager <<<\n"
                "# >>> llvm-manager >>>\ninterrupted\n",
                encoding="utf-8",
            )
            replace_managed_block(path, "# >>> llvm-manager >>>\nnew\n# <<< llvm-manager <<<")
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("# >>> llvm-manager >>>"), 1)
            self.assertNotIn("stale", text)
            self.assertNotIn("interrupted", text)
            self.assertIn("before", text)

    def test_strict_json_read_reports_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(LLVMManagerError):
                read_json(path, {}, strict=True)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertEqual(read_json(path, {}, warn=True), {})
            self.assertTrue(caught)


class BuildQualityTests(unittest.TestCase):
    def test_changed_configuration_cleans_stale_build_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary) / "build"
            _prepare_build_directory(build, {"projects": ["clang"]}, False)
            stale = build / "stale.o"
            stale.write_text("old", encoding="utf-8")
            _prepare_build_directory(build, {"projects": ["clang", "lld"]}, False)
            self.assertFalse(stale.exists())

    def test_install_verification_links_and_runs_native_programs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            bin_dir.mkdir()
            clang = executable(bin_dir / "clang-22")
            clangxx = executable(bin_dir / "clang++-22")
            commands: list[list[object]] = []

            def fake_run(command, **kwargs):
                del kwargs
                commands.append(list(command))
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    executable(output, "#!/bin/sh\nexit 0\n")
                return None

            with patch("llvm_mgr.builder.run", side_effect=fake_run):
                _verify_install(
                    prefix,
                    22,
                    {},
                    run_executables=True,
                    cxx_architectures=("arm64", "x86_64"),
                )

            compile_commands = [command for command in commands if "-o" in command]
            self.assertEqual(len(compile_commands), 4)
            self.assertTrue(all("-c" not in command for command in compile_commands))
            self.assertTrue(any(command[0] == clang for command in commands))
            self.assertTrue(any(command[0] == clangxx for command in commands))
            self.assertTrue(
                any(command[0] == clangxx and command[1:4] == ["-std=c++20", "-arch", "arm64"] for command in commands)
            )
            self.assertTrue(
                any(command[0] == clangxx and command[1:4] == ["-std=c++20", "-arch", "x86_64"] for command in commands)
            )


class ConfigQualityTests(unittest.TestCase):
    def test_workspace_argument_controls_default_root(self) -> None:
        workspace = Path("/tmp/llvm-manager-workspace")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(default_manager_root(workspace), workspace)

    def test_current_directory_is_the_fallback_workspace(self) -> None:
        workspace = Path("/tmp/llvm-manager-cwd")
        with patch.dict(os.environ, {}, clear=True), patch("llvm_mgr.config.Path.cwd", return_value=workspace):
            self.assertEqual(default_manager_root(), workspace)

    def test_environment_override_controls_default_workspace_root(self) -> None:
        with patch.dict(os.environ, {"LLVM_MANAGER_ROOT": "~/custom-llvm-root"}):
            self.assertEqual(default_manager_root(Path("/tmp/workspace")), Path("~/custom-llvm-root").expanduser())


if __name__ == "__main__":
    unittest.main()
