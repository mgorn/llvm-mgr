from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llvm_mgr.builder import (
    BuildOptions,
    WindowsLibXml2,
    _configure_command,
    _distribution_components,
    _host_cmake_options,
    _install_targets,
    _macos_managed_libcxx_configure_command,
    _normalized_tools,
    _primary_build_runtimes,
    _tool_selection_requires_libxml2,
    _verification_tools,
    _windows_libxml2_toolchain,
    _llvm_major,
)
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


class BuilderToolSelectionTests(unittest.TestCase):
    def _host(self) -> HostToolchain:
        return HostToolchain(
            "clang",
            "Clang",
            "clang",
            Path("/host/clang"),
            Path("/host/clang++"),
        )

    def _options(self, tools: tuple[str, ...] = ("all",)) -> BuildOptions:
        return BuildOptions(
            revision=SourceRevision(RevisionKind.TAG, "22.1.8"),
            install_prefix=Path("/install"),
            host_toolchain=self._host(),
            tools=tools,
        )

    def test_default_build_installs_all_configured_tools(self) -> None:
        options = self._options()
        command = _configure_command(
            "cmake",
            "ninja",
            Path("/source/llvm"),
            Path("/build"),
            Path("/install"),
            options,
        )

        self.assertEqual(_normalized_tools(options.tools), ("all",))
        self.assertEqual(_distribution_components(options), ())
        self.assertEqual(_install_targets(options, "linux"), ("install",))
        self.assertFalse(any(str(argument).startswith("-DLLVM_DISTRIBUTION_COMPONENTS=") for argument in command))

    def test_custom_tool_selection_keeps_clang_and_runtime_install(self) -> None:
        options = self._options(("lld", "llvm-objdump"))
        command = _configure_command(
            "cmake",
            "ninja",
            Path("/source/llvm"),
            Path("/build"),
            Path("/install"),
            options,
        )

        self.assertEqual(_normalized_tools(options.tools), ("clang", "lld", "llvm-objdump"))
        self.assertEqual(
            _distribution_components(options),
            ("clang", "clang-resource-headers", "lld", "llvm-objdump"),
        )
        self.assertEqual(_install_targets(options, "linux"), ("install-distribution", "install-runtimes"))
        self.assertIn(
            "-DLLVM_DISTRIBUTION_COMPONENTS=clang;clang-resource-headers;lld;llvm-objdump",
            command,
        )

    def test_mt_alias_normalizes_to_llvm_mt(self) -> None:
        self.assertEqual(_normalized_tools(("mt",)), ("clang", "llvm-mt"))

    def test_all_cannot_be_combined_with_individual_tools(self) -> None:
        with self.assertRaisesRegex(LLVMManagerError, "cannot be combined"):
            _normalized_tools(("all", "llvm-ar"))

    def test_windows_all_tools_forces_managed_libxml2_for_llvm_mt(self) -> None:
        options = self._options()
        libxml2 = WindowsLibXml2(Path("C:/libxml/include/libxml2"), Path("C:/libxml/lib/libxml2s.lib"))
        with patch("llvm_mgr.builder.sys.platform", "win32"):
            command = _configure_command(
                "cmake",
                "ninja",
                Path("C:/source/llvm"),
                Path("C:/build"),
                Path("C:/install"),
                options,
                libxml2,
            )

        self.assertTrue(_tool_selection_requires_libxml2(options, "win32"))
        self.assertIn("-DLLVM_ENABLE_LIBXML2=FORCE_ON", command)
        self.assertIn("-DLIBXML2_INCLUDE_DIR=C:/libxml/include/libxml2", command)
        self.assertIn("-DLIBXML2_LIBRARIES=C:/libxml/lib/libxml2s.lib", command)
        self.assertEqual(_verification_tools(options, "win32"), ("llvm-mt", "mt"))

    def test_windows_subset_without_llvm_mt_does_not_require_libxml2(self) -> None:
        options = self._options(("llvm-objdump",))
        self.assertFalse(_tool_selection_requires_libxml2(options, "win32"))
        self.assertEqual(_verification_tools(options, "win32"), ("clang", "llvm-objdump"))

    def test_windows_libxml2_reuses_msvc_style_host_toolchain(self) -> None:
        host = HostToolchain(
            "clang-cl-24",
            "ClangCL",
            "clang-cl",
            Path("C:/LLVM/clang-cl.exe"),
            Path("C:/LLVM/clang-cl.exe"),
            version="24.0.0",
        )
        with patch("llvm_mgr.builder.discover_host_toolchains") as discover:
            selected = _windows_libxml2_toolchain(host)

        self.assertIs(selected, host)
        discover.assert_not_called()

    def test_windows_libxml2_uses_msvc_compatible_compiler_for_gnu_clang(self) -> None:
        host = HostToolchain(
            "clang-23",
            "Clang",
            "clang",
            Path("C:/LLVM/clang-23.exe"),
            Path("C:/LLVM/clang++-23.exe"),
            version="23.0.0",
        )
        sibling_clang_cl = HostToolchain(
            "clang-cl-24",
            "ClangCL",
            "clang-cl",
            Path("C:/LLVM/clang-cl.exe"),
            Path("C:/LLVM/clang-cl.exe"),
            version="24.0.0",
        )
        msvc = HostToolchain(
            "msvc-19",
            "MSVC",
            "msvc",
            Path("C:/VS/cl.exe"),
            Path("C:/VS/cl.exe"),
            version="19.44",
        )
        with patch(
            "llvm_mgr.builder.discover_host_toolchains",
            return_value=[msvc, sibling_clang_cl, host],
        ):
            selected = _windows_libxml2_toolchain(host)

        self.assertIs(selected, sibling_clang_cl)

    def test_windows_libxml2_reports_missing_msvc_compatible_compiler(self) -> None:
        host = HostToolchain(
            "gcc-15",
            "GCC",
            "gcc",
            Path("C:/GCC/gcc.exe"),
            Path("C:/GCC/g++.exe"),
            version="15.1.0",
        )
        with (
            patch("llvm_mgr.builder.discover_host_toolchains", return_value=[host]),
            self.assertRaisesRegex(LLVMManagerError, "MSVC-compatible compiler"),
        ):
            _windows_libxml2_toolchain(host)


class BuilderStandardLibraryTests(unittest.TestCase):
    def test_macos_managed_libcxx_is_split_from_the_primary_runtime_build(self) -> None:
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

        self.assertEqual(_primary_build_runtimes(options, "darwin"), ("compiler-rt",))
        self.assertIn("-DLLVM_ENABLE_RUNTIMES=compiler-rt", command)
        self.assertNotIn("-DLIBCXXABI_USE_LLVM_UNWINDER=ON", command)
        self.assertIn("-DCLANG_USE_XCSELECT=ON", command)

    def test_macos_managed_libcxx_runtime_build_is_universal(self) -> None:
        command = _macos_managed_libcxx_configure_command(
            "cmake",
            "ninja",
            Path("/source/runtimes"),
            Path("/build/managed-libcxx-universal"),
            Path("/install"),
            "Release",
        )

        self.assertIn("-DCMAKE_C_COMPILER=/install/bin/clang", command)
        self.assertIn("-DCMAKE_CXX_COMPILER=/install/bin/clang++", command)
        self.assertIn("-DCMAKE_CXX_FLAGS=--no-default-config", command)
        self.assertIn("-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64", command)
        self.assertIn("-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind", command)
        self.assertIn("-DLIBCXXABI_USE_LLVM_UNWINDER=ON", command)


if __name__ == "__main__":
    unittest.main()
