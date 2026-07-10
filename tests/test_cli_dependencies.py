from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from llvm_mgr.cli import main
from llvm_mgr.dependencies import DependencyInstallPlan, DependencyReport, ProgramDependency


class BuildDependencyGateTests(unittest.TestCase):
    def test_build_stops_before_fetching_tags_when_dependencies_are_missing(self) -> None:
        report = DependencyReport(
            programs=(
                ProgramDependency(
                    identifier="git",
                    name="Git",
                    purpose="fetch and check out LLVM source",
                    executable="git",
                    path=None,
                ),
            ),
            toolchains=(),
            install_hint="Install the missing dependencies.",
        )
        output = io.StringIO()
        errors = io.StringIO()

        with (
            patch("llvm_mgr.cli.check_build_dependencies", return_value=report),
            patch("llvm_mgr.cli.fetch_release_tags") as fetch_release_tags,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = main(["build", "22.1.8"])

        self.assertEqual(result, 1)
        fetch_release_tags.assert_not_called()
        self.assertIn("[MISSING] Git", output.getvalue())
        self.assertIn("cannot be built", errors.getvalue())

    def test_build_offers_installer_and_stops_when_user_declines(self) -> None:
        report = DependencyReport(
            programs=(
                ProgramDependency(
                    identifier="cmake",
                    name="CMake",
                    purpose="configure the LLVM build",
                    executable="cmake",
                    path=None,
                ),
            ),
            toolchains=(),
            install_hint="Install CMake.",
            install_plan=DependencyInstallPlan(
                manager="test",
                commands=(("installer", "cmake"),),
                requires_elevation=True,
            ),
        )
        output = io.StringIO()
        errors = io.StringIO()

        with (
            patch("llvm_mgr.cli.check_build_dependencies", return_value=report),
            patch("llvm_mgr.cli.fetch_release_tags") as fetch_release_tags,
            patch("llvm_mgr.cli.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = main(["build", "22.1.8"])

        self.assertEqual(result, 1)
        fetch_release_tags.assert_not_called()
        self.assertIn("installer cmake", output.getvalue())
        self.assertIn("sudo", output.getvalue())
        self.assertIn("skipped", output.getvalue())



if __name__ == "__main__":
    unittest.main()
