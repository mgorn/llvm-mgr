from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llvm_mgr.cli import _menu, _select_llvm_tools
from llvm_mgr.config import ManagerPaths
from llvm_mgr.models import InstallInfo
from llvm_mgr.versioning import LLVMVersion


class BuildToolMenuTests(unittest.TestCase):
    def test_defaults_to_all_llvm_tools(self) -> None:
        with (
            patch("llvm_mgr.cli._prompt", return_value="all"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(_select_llvm_tools(), ("all",))

    def test_accepts_a_comma_separated_tool_subset(self) -> None:
        with (
            patch("llvm_mgr.cli._prompt", return_value="lld,llvm-objdump,llvm-mt"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(_select_llvm_tools(), ("lld", "llvm-objdump", "llvm-mt"))


class MainMenuStandardLibraryTests(unittest.TestCase):
    def test_shows_standard_library_switch_for_active_managed_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            install = InstallInfo(
                base / "llvm-22",
                base / "llvm-22" / "bin" / "clang",
                LLVMVersion(22, 1, 8),
                True,
                active=True,
            )
            output = io.StringIO()
            with (
                patch("llvm_mgr.cli.sys.stdin.isatty", return_value=True),
                patch("llvm_mgr.cli.scan_installs", return_value=[install]),
                patch("llvm_mgr.cli._prompt", return_value="5"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(_menu(paths, "unused"), 0)

            self.assertIn("4) Switch C++ standard library", output.getvalue())
            self.assertIn("5) Exit", output.getvalue())

    def test_hides_standard_library_switch_for_active_external_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            install = InstallInfo(
                base / "llvm-22",
                base / "llvm-22" / "bin" / "clang",
                LLVMVersion(22, 1, 8),
                False,
                active=True,
            )
            output = io.StringIO()
            with (
                patch("llvm_mgr.cli.sys.stdin.isatty", return_value=True),
                patch("llvm_mgr.cli.scan_installs", return_value=[install]),
                patch("llvm_mgr.cli._prompt", return_value="4"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(_menu(paths, "unused"), 0)

            self.assertNotIn("Switch C++ standard library", output.getvalue())
            self.assertIn("4) Exit", output.getvalue())

    def test_standard_library_menu_dispatches_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            active = InstallInfo(
                base / "llvm-22",
                base / "llvm-22" / "bin" / "clang",
                LLVMVersion(22, 1, 8),
                True,
                active=True,
            )
            provider = InstallInfo(
                base / "llvm-21",
                base / "llvm-21" / "bin" / "clang",
                LLVMVersion(21, 1, 8),
                True,
                managed_libcxx_available=True,
            )
            with (
                patch("llvm_mgr.cli.sys.stdin.isatty", return_value=True),
                patch("llvm_mgr.cli.scan_installs", return_value=[active, provider]),
                patch("llvm_mgr.cli._prompt", side_effect=["4", "2", "5"]),
                patch("llvm_mgr.cli.switch_standard_library") as switch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(_menu(paths, "unused"), 0)

            switch.assert_called_once_with(paths, active, provider)


if __name__ == "__main__":
    unittest.main()
