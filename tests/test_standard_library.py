from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from llvm_mgr.standard_library import (
    MANAGED_LIBCXX_STANDARD_LIBRARY,
    SYSTEM_CXX_STANDARD_LIBRARY,
    configure_standard_library,
    standard_library_cmake_options,
    standard_library_runtimes,
)


def executable(path: Path, contents: str) -> Path:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)
    return path


def macho_library(path: Path, architectures: tuple[str, ...]) -> Path:
    cpu_types = {"x86_64": 0x01000007, "arm64": 0x0100000C}
    if len(architectures) == 1:
        path.write_bytes(
            b"\xcf\xfa\xed\xfe"
            + struct.pack("<I", cpu_types[architectures[0]])
            + b"\0" * 24
        )
        return path

    header = bytearray(b"\xca\xfe\xba\xbe")
    header.extend(struct.pack(">I", len(architectures)))
    for architecture in architectures:
        header.extend(struct.pack(">IIIII", cpu_types[architecture], 0, 0, 0, 0))
    path.write_bytes(header)
    return path


class StandardLibrarySelectionTests(unittest.TestCase):
    def test_system_selection_preserves_requested_runtimes(self) -> None:
        self.assertEqual(
            standard_library_runtimes(
                SYSTEM_CXX_STANDARD_LIBRARY,
                ("compiler-rt", "compiler-rt"),
            ),
            ("compiler-rt",),
        )
        self.assertEqual(standard_library_cmake_options(SYSTEM_CXX_STANDARD_LIBRARY), ())

    def test_managed_libcxx_adds_complete_runtime_stack(self) -> None:
        self.assertEqual(
            standard_library_runtimes(
                MANAGED_LIBCXX_STANDARD_LIBRARY,
                ("compiler-rt", "libcxx"),
            ),
            ("compiler-rt", "libcxx", "libcxxabi", "libunwind"),
        )
        self.assertEqual(
            standard_library_cmake_options(MANAGED_LIBCXX_STANDARD_LIBRARY),
            ("-DLIBCXXABI_USE_LLVM_UNWINDER=ON",),
        )

    @unittest.skipIf(__import__("os").name == "nt", "POSIX compiler fixture")
    def test_managed_libcxx_writes_relocatable_target_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="llvm manager stdlib ") as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            headers = prefix / "include" / "c++" / "v1"
            libraries = prefix / "lib" / "arm64-apple-darwin25.5.0"
            bin_dir.mkdir(parents=True)
            headers.mkdir(parents=True)
            libraries.mkdir(parents=True)
            (libraries / "libc++.dylib").write_text("", encoding="utf-8")
            executable(
                bin_dir / "clang++-22",
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--print-target-triple\" ]; then\n"
                "  printf '%s\\n' 'arm64-apple-darwin25.5.0'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
            )

            metadata, files = configure_standard_library(
                prefix,
                22,
                {},
                MANAGED_LIBCXX_STANDARD_LIBRARY,
            )

            config = bin_dir / "arm64-apple-darwin25.5.0-clang++.cfg"
            self.assertEqual(files, [config])
            self.assertEqual(metadata["kind"], MANAGED_LIBCXX_STANDARD_LIBRARY)
            self.assertEqual(metadata["target"], "arm64-apple-darwin25.5.0")
            self.assertEqual(metadata["libraries"], "lib/arm64-apple-darwin25.5.0")
            contents = config.read_text(encoding="utf-8")
            self.assertIn("-nostdinc++", contents)
            self.assertIn("-isystem <CFGDIR>/../include/c++/v1", contents)
            self.assertNotIn("-nostdlib++", contents)
            self.assertIn("-L <CFGDIR>/../lib/arm64-apple-darwin25.5.0", contents)
            self.assertIn("-Wl,-rpath,<CFGDIR>/../lib/arm64-apple-darwin25.5.0", contents)

    @unittest.skipIf(__import__("os").name == "nt", "POSIX compiler fixture")
    def test_managed_libcxx_accepts_per_target_header_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            triple = "x86_64-unknown-linux-gnu"
            bin_dir = prefix / "bin"
            headers = prefix / "include" / triple / "c++" / "v1"
            libraries = prefix / "lib"
            bin_dir.mkdir(parents=True)
            headers.mkdir(parents=True)
            libraries.mkdir(parents=True)
            (libraries / "libc++.so").write_text("", encoding="utf-8")
            executable(
                bin_dir / "clang++-22",
                f"#!/bin/sh\nprintf '%s\\n' '{triple}'\n",
            )

            metadata, _ = configure_standard_library(
                prefix,
                22,
                {},
                MANAGED_LIBCXX_STANDARD_LIBRARY,
            )

            self.assertEqual(metadata["headers"], f"include/{triple}/c++/v1")

    @unittest.skipIf(__import__("os").name == "nt", "POSIX compiler fixture")
    def test_managed_libcxx_writes_configs_for_all_runtime_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            headers = prefix / "include" / "c++" / "v1"
            libraries = prefix / "lib"
            bin_dir.mkdir(parents=True)
            headers.mkdir(parents=True)
            libraries.mkdir(parents=True)
            macho_library(libraries / "libc++.dylib", ("arm64", "x86_64"))
            executable(
                bin_dir / "clang++-22",
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-arch\" ] && [ \"$3\" = \"--print-target-triple\" ]; then\n"
                "  printf '%s-apple-darwin25.5.0\\n' \"$2\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = \"--print-target-triple\" ]; then\n"
                "  printf '%s\\n' 'arm64-apple-darwin25.5.0'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
            )

            metadata, files = configure_standard_library(
                prefix,
                22,
                {},
                MANAGED_LIBCXX_STANDARD_LIBRARY,
                architectures=("arm64", "x86_64"),
            )

            self.assertEqual(metadata["architectures"], ["arm64", "x86_64"])
            self.assertEqual(
                metadata["targets"],
                ["arm64-apple-darwin25.5.0", "x86_64-apple-darwin25.5.0"],
            )
            self.assertEqual(len(files), 2)
            self.assertTrue((bin_dir / "arm64-apple-darwin25.5.0-clang++.cfg").is_file())
            self.assertTrue((bin_dir / "x86_64-apple-darwin25.5.0-clang++.cfg").is_file())


    @unittest.skipIf(__import__("os").name == "nt", "POSIX compiler fixture")
    def test_managed_libcxx_rejects_missing_universal_slice(self) -> None:
        from llvm_mgr.util import LLVMManagerError

        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            bin_dir = prefix / "bin"
            headers = prefix / "include" / "c++" / "v1"
            libraries = prefix / "lib"
            bin_dir.mkdir(parents=True)
            headers.mkdir(parents=True)
            libraries.mkdir(parents=True)
            macho_library(libraries / "libc++.dylib", ("arm64",))
            executable(
                bin_dir / "clang++-22",
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--print-target-triple\" ]; then\n"
                "  printf '%s\\n' 'arm64-apple-darwin25.5.0'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
            )

            with self.assertRaisesRegex(LLVMManagerError, "x86_64"):
                configure_standard_library(
                    prefix,
                    22,
                    {},
                    MANAGED_LIBCXX_STANDARD_LIBRARY,
                    architectures=("arm64", "x86_64"),
                )


@unittest.skipIf(__import__("os").name == "nt", "Managed libc++ pairing is POSIX-only")
class StandardLibrarySwitchTests(unittest.TestCase):
    def _make_compiler(self, prefix: Path, version: str = "22.1.8") -> Path:
        bin_dir = prefix / "bin"
        bin_dir.mkdir(parents=True)
        clang = executable(
            bin_dir / "clang-22",
            f"#!/bin/sh\n"
            f"if [ \"$1\" = \"--version\" ]; then echo 'clang version {version}'; exit 0; fi\n"
            "if [ \"$1\" = \"-arch\" ] && [ \"$3\" = \"--print-target-triple\" ]; then\n"
            "  echo \"$2-apple-darwin25.5.0\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"--print-target-triple\" ] || [ \"$1\" = \"-dumpmachine\" ]; then\n"
            "  echo 'arm64-apple-darwin25.5.0'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        (bin_dir / "clang++-22").symlink_to(clang.name)
        return clang

    def _make_provider(
        self,
        prefix: Path,
        version: str,
        *,
        architectures: tuple[str, ...] = (),
    ) -> None:
        headers = prefix / "include" / "c++" / "v1"
        libraries = prefix / "lib" / "arm64-apple-darwin25.5.0"
        headers.mkdir(parents=True)
        libraries.mkdir(parents=True)
        if architectures:
            macho_library(libraries / "libc++.dylib", architectures)
        else:
            (libraries / "libc++.dylib").write_text("", encoding="utf-8")
        (prefix / ".llvm-manager.json").write_text(
            "{\n"
            '  "schema_version": 4,\n'
            f'  "source": {{"kind": "tag", "value": "llvmorg-{version}"}},\n'
            '  "cxx_standard_library": {"kind": "system"},\n'
            '  "managed_cxx_standard_library": {\n'
            '    "kind": "managed-libc++",\n'
            '    "target": "arm64-apple-darwin25.5.0",\n'
            '    "headers": "include/c++/v1",\n'
            '    "libraries": "lib/arm64-apple-darwin25.5.0",\n'
            '    "runtimes": ["libcxx", "libcxxabi", "libunwind"]'
            + (',\n    "architectures": ["arm64", "x86_64"]\n' if architectures else '\n')
            + "  }\n"
            "}\n",
            encoding="utf-8",
        )

    def test_switches_active_compiler_to_another_managed_libcxx_version(self) -> None:
        import json

        from llvm_mgr.config import ManagerPaths
        from llvm_mgr.models import InstallInfo
        from llvm_mgr.standard_library import switch_standard_library
        from llvm_mgr.versioning import LLVMVersion

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            paths.ensure_root()
            compiler_prefix = paths.install_root / "llvmorg-22.1.8"
            provider_prefix = paths.install_root / "llvm 21.1.8"
            compiler = self._make_compiler(compiler_prefix)
            self._make_provider(provider_prefix, "21.1.8")
            (compiler_prefix / ".llvm-manager.json").write_text(
                '{"schema_version":4,"source":{"kind":"tag","value":"llvmorg-22.1.8"},'
                '"cxx_standard_library":{"kind":"system"},"installed_files":[]}\n',
                encoding="utf-8",
            )
            compiler_install = InstallInfo(
                compiler_prefix,
                compiler,
                LLVMVersion(22, 1, 8),
                True,
                active=True,
                tag="llvmorg-22.1.8",
                cxx_standard_library="system",
            )
            provider_install = InstallInfo(
                provider_prefix,
                provider_prefix / "bin" / "clang-21",
                LLVMVersion(21, 1, 8),
                True,
                tag="llvmorg-21.1.8",
                managed_libcxx_available=True,
            )

            selected = switch_standard_library(paths, compiler_install, provider_install, env={})

            self.assertEqual(selected["provider_version"], "21.1.8")
            self.assertEqual(selected["provider_prefix"], "../llvm 21.1.8")
            config = compiler_prefix / selected["config"]
            contents = config.read_text(encoding="utf-8")
            self.assertIn('"<CFGDIR>/../../llvm 21.1.8/include/c++/v1"', contents)
            self.assertIn('"<CFGDIR>/../../llvm 21.1.8/lib/arm64-apple-darwin25.5.0"', contents)
            metadata = json.loads((compiler_prefix / ".llvm-manager.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["cxx_standard_library"]["provider_version"], "21.1.8")
            state = json.loads(paths.state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["cxx_standard_library"], "managed-libc++")
            self.assertEqual(state["cxx_standard_library_version"], "21.1.8")

            switch_standard_library(paths, compiler_install, None, env={})

            self.assertFalse(config.exists())
            metadata = json.loads((compiler_prefix / ".llvm-manager.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["cxx_standard_library"], {"kind": "system"})

    def test_switches_universal_provider_configs_and_removes_both(self) -> None:
        import json

        from llvm_mgr.config import ManagerPaths
        from llvm_mgr.models import InstallInfo
        from llvm_mgr.standard_library import switch_standard_library
        from llvm_mgr.versioning import LLVMVersion

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            paths.ensure_root()
            compiler_prefix = paths.install_root / "llvmorg-22.1.8"
            provider_prefix = paths.install_root / "llvmorg-21.1.8"
            compiler = self._make_compiler(compiler_prefix)
            self._make_provider(
                provider_prefix,
                "21.1.8",
                architectures=("arm64", "x86_64"),
            )
            (compiler_prefix / ".llvm-manager.json").write_text(
                '{"schema_version":4,"source":{"kind":"tag","value":"llvmorg-22.1.8"},'
                '"cxx_standard_library":{"kind":"system"},"installed_files":[]}\n',
                encoding="utf-8",
            )
            compiler_install = InstallInfo(
                compiler_prefix,
                compiler,
                LLVMVersion(22, 1, 8),
                True,
                active=True,
                tag="llvmorg-22.1.8",
                cxx_standard_library="system",
            )
            provider_install = InstallInfo(
                provider_prefix,
                provider_prefix / "bin" / "clang-21",
                LLVMVersion(21, 1, 8),
                True,
                tag="llvmorg-21.1.8",
                managed_libcxx_available=True,
            )

            selected = switch_standard_library(paths, compiler_install, provider_install, env={})

            self.assertEqual(selected["architectures"], ["arm64", "x86_64"])
            self.assertEqual(
                selected["targets"],
                ["arm64-apple-darwin25.5.0", "x86_64-apple-darwin25.5.0"],
            )
            configs = [compiler_prefix / value for value in selected["configs"]]
            self.assertEqual(len(configs), 2)
            self.assertTrue(all(config.is_file() for config in configs))
            metadata = json.loads((compiler_prefix / ".llvm-manager.json").read_text(encoding="utf-8"))
            self.assertTrue(all(value in metadata["installed_files"] for value in selected["configs"]))

            switch_standard_library(paths, compiler_install, None, env={})

            self.assertTrue(all(not config.exists() for config in configs))
            metadata = json.loads((compiler_prefix / ".llvm-manager.json").read_text(encoding="utf-8"))
            self.assertTrue(all(value not in metadata["installed_files"] for value in selected["configs"]))

    def test_switching_to_system_preserves_locally_installed_libcxx(self) -> None:
        import json

        from llvm_mgr.config import ManagerPaths
        from llvm_mgr.discovery import inspect_install
        from llvm_mgr.models import InstallInfo
        from llvm_mgr.standard_library import configure_standard_library, switch_standard_library
        from llvm_mgr.versioning import LLVMVersion

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            paths.ensure_root()
            prefix = paths.install_root / "llvmorg-22.1.8"
            clang = self._make_compiler(prefix)
            headers = prefix / "include" / "c++" / "v1"
            libraries = prefix / "lib" / "arm64-apple-darwin25.5.0"
            headers.mkdir(parents=True)
            libraries.mkdir(parents=True)
            (libraries / "libc++.dylib").write_text("", encoding="utf-8")
            selected, _ = configure_standard_library(
                prefix,
                22,
                {},
                MANAGED_LIBCXX_STANDARD_LIBRARY,
            )
            (prefix / ".llvm-manager.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "source": {"kind": "tag", "value": "llvmorg-22.1.8"},
                        "cxx_standard_library": selected,
                        "installed_files": [selected["config"]],
                    }
                ),
                encoding="utf-8",
            )
            install = InstallInfo(
                prefix,
                clang,
                LLVMVersion(22, 1, 8),
                True,
                active=True,
                tag="llvmorg-22.1.8",
                cxx_standard_library="managed-libc++",
                managed_libcxx_available=True,
            )

            switch_standard_library(paths, install, None, env={})

            metadata = json.loads((prefix / ".llvm-manager.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["cxx_standard_library"], {"kind": "system"})
            self.assertEqual(metadata["managed_cxx_standard_library"]["kind"], "managed-libc++")
            rediscovered = inspect_install(prefix)
            self.assertIsNotNone(rediscovered)
            self.assertTrue(rediscovered.managed_libcxx_available)
            self.assertEqual(rediscovered.cxx_standard_library, "system")

    def test_state_write_failure_restores_previous_pairing(self) -> None:
        import json
        from unittest.mock import patch

        from llvm_mgr.config import ManagerPaths
        from llvm_mgr.models import InstallInfo
        from llvm_mgr.standard_library import configure_standard_library, switch_standard_library
        from llvm_mgr.versioning import LLVMVersion

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = ManagerPaths.create(base / "manager", base / "home")
            paths.ensure_root()
            prefix = paths.install_root / "llvmorg-22.1.8"
            clang = self._make_compiler(prefix)
            headers = prefix / "include" / "c++" / "v1"
            libraries = prefix / "lib" / "arm64-apple-darwin25.5.0"
            headers.mkdir(parents=True)
            libraries.mkdir(parents=True)
            (libraries / "libc++.dylib").write_text("", encoding="utf-8")
            selected, _ = configure_standard_library(
                prefix,
                22,
                {},
                MANAGED_LIBCXX_STANDARD_LIBRARY,
            )
            metadata_path = prefix / ".llvm-manager.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "source": {"kind": "tag", "value": "llvmorg-22.1.8"},
                        "cxx_standard_library": selected,
                        "managed_cxx_standard_library": {
                            "kind": "managed-libc++",
                            "target": "arm64-apple-darwin25.5.0",
                            "headers": "include/c++/v1",
                            "libraries": "lib/arm64-apple-darwin25.5.0",
                            "runtimes": ["libcxx", "libcxxabi", "libunwind"],
                        },
                        "installed_files": [selected["config"]],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            original_metadata = metadata_path.read_text(encoding="utf-8")
            config = prefix / selected["config"]
            original_config = config.read_text(encoding="utf-8")
            install = InstallInfo(
                prefix,
                clang,
                LLVMVersion(22, 1, 8),
                True,
                active=True,
                tag="llvmorg-22.1.8",
                cxx_standard_library="managed-libc++",
                managed_libcxx_available=True,
            )

            with patch(
                "llvm_mgr.standard_library.write_json",
                side_effect=[None, OSError("disk full")],
            ):
                with self.assertRaises(OSError):
                    switch_standard_library(paths, install, None, env={})

            self.assertEqual(metadata_path.read_text(encoding="utf-8"), original_metadata)
            self.assertEqual(config.read_text(encoding="utf-8"), original_config)
            self.assertFalse(paths.state_file.exists())


if __name__ == "__main__":
    unittest.main()
