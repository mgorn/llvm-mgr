from __future__ import annotations

import os
import tempfile
import contextlib
import io
import unittest
from pathlib import Path

from unittest.mock import patch

from llvm_mgr.dependencies import (
    DependencyInstallPlan,
    DependencyReport,
    ProgramDependency,
    check_build_dependencies,
    offer_dependency_install,
    require_build_dependencies,
    run_dependency_install,
)
from llvm_mgr.toolchains import HostToolchain
from llvm_mgr.util import LLVMManagerError


@unittest.skipIf(os.name == "nt", "POSIX executable fixture")
class DependencyCheckTests(unittest.TestCase):
    def _program(self, directory: Path, name: str, version: str) -> Path:
        path = directory / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{name} version {version}'\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _compiler(self, directory: Path) -> None:
        compiler = directory / "clang"
        compiler.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'clang version 18.1.8'\n"
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
        compiler.chmod(0o755)
        (directory / "clang++").symlink_to("clang")

    def test_reports_ready_when_all_build_dependencies_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            self._program(binary_dir, "git", "2.45.2")
            self._program(binary_dir, "cmake", "3.30.1")
            self._program(binary_dir, "ninja", "1.12.1")
            self._compiler(binary_dir)

            report = check_build_dependencies(str(binary_dir))

            self.assertTrue(report.ready)
            self.assertEqual(report.missing_names, ())
            self.assertEqual({item.identifier for item in report.programs}, {"git", "cmake", "ninja"})
            self.assertEqual(len(report.toolchains), 1)
            self.assertEqual(report.toolchains[0].family, "clang")
            require_build_dependencies(report)

    def test_reports_every_missing_required_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = check_build_dependencies(temporary)

            self.assertFalse(report.ready)
            self.assertEqual(
                report.missing_names,
                ("Git", "CMake", "Ninja", "usable C/C++ compiler toolchain"),
            )
            self.assertIn("Suggested command", report.install_hint)
            with self.assertRaises(LLVMManagerError):
                require_build_dependencies(report)

    def test_json_contains_program_and_compiler_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            self._program(binary_dir, "git", "2.45.2")

            data = check_build_dependencies(str(binary_dir)).to_json()

            self.assertFalse(data["ready"])
            self.assertEqual(data["programs"][0]["name"], "Git")
            self.assertTrue(data["programs"][0]["found"])
            self.assertIn("CMake", data["missing"])
            self.assertEqual(data["toolchains"], [])


    def test_install_plan_runs_each_command_without_shell_composition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / "installer.log"
            installer = directory / "installer"
            installer.write_text(
                "#!/bin/sh\n"
                f"printf '%s\n' \"$1\" >> '{log}'\n",
                encoding="utf-8",
            )
            installer.chmod(0o755)
            plan = DependencyInstallPlan(
                manager="test",
                commands=((str(installer), "first"), (str(installer), "second")),
                requires_elevation=False,
            )

            run_dependency_install(plan)

            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["first", "second"])

    def test_declining_install_plan_does_not_run_it(self) -> None:
        report = DependencyReport(
            programs=(
                ProgramDependency("cmake", "CMake", "configure LLVM", "cmake", None),
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

        with (
            patch("llvm_mgr.dependencies.run_dependency_install") as installer,
            contextlib.redirect_stdout(output),
        ):
            result = offer_dependency_install(report, input_fn=lambda _: "n")

        self.assertIs(result, report)
        installer.assert_not_called()
        self.assertIn("installer cmake", output.getvalue())
        self.assertIn("sudo", output.getvalue())
        self.assertIn("skipped", output.getvalue())

    def test_confirming_install_plan_runs_and_rechecks(self) -> None:
        missing = DependencyReport(
            programs=(
                ProgramDependency("cmake", "CMake", "configure LLVM", "cmake", None),
            ),
            toolchains=(),
            install_hint="Install CMake.",
            install_plan=DependencyInstallPlan(
                manager="test",
                commands=(("installer", "cmake"),),
                requires_elevation=False,
            ),
        )
        ready = DependencyReport(
            programs=(
                ProgramDependency("cmake", "CMake", "configure LLVM", "cmake", Path("/tools/cmake")),
            ),
            toolchains=(
                HostToolchain(
                    identifier="clang-18",
                    name="Clang",
                    family="clang",
                    cc=Path("/tools/clang"),
                    cxx=Path("/tools/clang++"),
                    version="18.1.8",
                ),
            ),
        )

        with (
            patch("llvm_mgr.dependencies.run_dependency_install") as installer,
            patch("llvm_mgr.dependencies.check_build_dependencies", return_value=ready) as recheck,
        ):
            result = offer_dependency_install(missing, input_fn=lambda _: "yes")

        installer.assert_called_once_with(missing.install_plan)
        recheck.assert_called_once_with(None)
        self.assertIs(result, ready)



if __name__ == "__main__":
    unittest.main()
