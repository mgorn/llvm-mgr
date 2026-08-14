from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llvm_mgr.builder import BuildOptions, _configure_command, _host_cmake_options, _llvm_major
from llvm_mgr.repository import RevisionKind, SourceRevision
from llvm_mgr.standard_library import MANAGED_LIBCXX_STANDARD_LIBRARY
from llvm_mgr.toolchains import HostToolchain
from llvm_mgr.util import LLVMManagerError


class BuilderVersionTests(unittest.TestCase):
    def test_reads_modern_monorepo_version_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            llvm = root / "llvm"
            llvm.mkdir()
            module = root / "cmake" / "Modules" / "LLVMVersion.cmake"
            module.parent.mkdir(parents=True)
            module.write_text(
                "if(NOT DEFINED LLVM_VERSION_MAJOR)\n"
                "  set(LLVM_VERSION_MAJOR 23)\n"
                "endif()\n",
                encoding="utf-8",
            )
            self.assertEqual(_llvm_major(llvm), 23)

    def test_keeps_cmakelists_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            llvm = Path(temporary) / "llvm"
            llvm.mkdir()
            (llvm / "CMakeLists.txt").write_text(
                "set(LLVM_VERSION_MAJOR 22)\n",
                encoding="utf-8",
            )
            self.assertEqual(_llvm_major(llvm), 22)

    def test_error_lists_searched_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            llvm = Path(temporary) / "llvm"
            llvm.mkdir()
            with self.assertRaisesRegex(LLVMManagerError, "LLVMVersion\\.cmake"):
                _llvm_major(llvm)


class BuilderPlatformTests(unittest.TestCase):
    def test_enables_xcselect_on_macos(self) -> None:
        self.assertEqual(_host_cmake_options("darwin"), ("-DCLANG_USE_XCSELECT=ON",))

    def test_does_not_enable_xcselect_on_other_platforms(self) -> None:
        self.assertEqual(_host_cmake_options("linux"), ())
        self.assertEqual(_host_cmake_options("win32"), ())


class BuilderTargetTests(unittest.TestCase):
    def _host(self) -> HostToolchain:
        return HostToolchain(
            "clang",
            "Clang",
            "clang",
            Path("/host/clang"),
            Path("/host/clang++"),
        )

    def test_default_build_enables_all_targets(self) -> None:
        options = BuildOptions(
            revision=SourceRevision(RevisionKind.TAG, "22.1.8"),
            install_prefix=Path("/install"),
            host_toolchain=self._host(),
        )

        command = _configure_command(
            "cmake",
            "ninja",
            Path("/source/llvm"),
            Path("/build"),
            Path("/install"),
            options,
        )

        self.assertEqual(options.targets, "all")
        self.assertIn("-DLLVM_TARGETS_TO_BUILD=all", command)

    def test_native_target_remains_available_as_an_override(self) -> None:
        options = BuildOptions(
            revision=SourceRevision(RevisionKind.TAG, "22.1.8"),
            install_prefix=Path("/install"),
            host_toolchain=self._host(),
            targets="Native",
        )

        command = _configure_command(
            "cmake",
            "ninja",
            Path("/source/llvm"),
            Path("/build"),
            Path("/install"),
            options,
        )

        self.assertIn("-DLLVM_TARGETS_TO_BUILD=Native", command)


class BuilderStandardLibraryTests(unittest.TestCase):
    def test_managed_libcxx_is_added_to_the_runtimes_build(self) -> None:
        host = HostToolchain(
            "clang",
            "Clang",
            "clang",
            Path("/host/clang"),
            Path("/host/clang++"),
        )
        options = BuildOptions(
            revision=SourceRevision(RevisionKind.TAG, "22.1.8"),
            install_prefix=Path("/install"),
            host_toolchain=host,
            cxx_standard_library=MANAGED_LIBCXX_STANDARD_LIBRARY,
        )
        with patch("llvm_mgr.builder.sys.platform", "darwin"):
            command = _configure_command(
                "cmake",
                "ninja",
                Path("/source/llvm"),
                Path("/build"),
                Path("/install"),
                options,
            )

        self.assertIn(
            "-DLLVM_ENABLE_RUNTIMES=compiler-rt;libcxx;libcxxabi;libunwind",
            command,
        )
        self.assertIn("-DLIBCXXABI_USE_LLVM_UNWINDER=ON", command)
        self.assertIn("-DCLANG_USE_XCSELECT=ON", command)


if __name__ == "__main__":
    unittest.main()
