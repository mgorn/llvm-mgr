from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llvm_mgr.toolchains import discover_host_toolchains, select_host_toolchain
from llvm_mgr.util import LLVMManagerError


@unittest.skipIf(os.name == "nt", "POSIX executable fixture")
class ToolchainDiscoveryTests(unittest.TestCase):
    def _compiler(self, directory: Path, name: str, version_line: str) -> Path:
        path = directory / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{version_line}'\n"
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
        path.chmod(0o755)
        return path

    def test_discovers_versioned_gcc_and_apple_clang_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            clang = self._compiler(binary_dir, "clang", "Apple clang version 16.0.0")
            (binary_dir / "clang++").symlink_to(clang.name)
            gcc = self._compiler(binary_dir, "gcc-13", "gcc (GCC) 13.2.1")
            (binary_dir / "g++-13").symlink_to(gcc.name)

            toolchains = discover_host_toolchains(str(binary_dir))

            self.assertEqual({item.family for item in toolchains}, {"apple-clang", "gcc"})
            apple = next(item for item in toolchains if item.family == "apple-clang")
            gnu = next(item for item in toolchains if item.family == "gcc")
            self.assertEqual(apple.version, "16.0.0")
            self.assertEqual(gnu.version, "13.2.1")
            self.assertEqual(apple.cxx.name, "clang++")
            self.assertEqual(gnu.cxx.name, "g++-13")

    def test_default_discovery_uses_augmented_windows_search_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            clang = self._compiler(binary_dir, "clang", "clang version 18.1.8")
            (binary_dir / "clang++").symlink_to(clang.name)

            with mock.patch(
                "llvm_mgr.toolchains.augment_windows_search_path",
                return_value=str(binary_dir),
            ) as augment:
                toolchains = discover_host_toolchains()

            augment.assert_called_once_with(None)
            self.assertEqual(len(toolchains), 1)
            self.assertEqual(toolchains[0].family, "clang")

    def test_selects_by_number_id_family_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            clang = self._compiler(binary_dir, "clang", "clang version 18.1.8")
            (binary_dir / "clang++").symlink_to(clang.name)
            toolchains = discover_host_toolchains(str(binary_dir))

            selected = toolchains[0]
            self.assertEqual(select_host_toolchain(toolchains, "1"), selected)
            self.assertEqual(select_host_toolchain(toolchains, selected.identifier), selected)
            self.assertEqual(select_host_toolchain(toolchains, "clang"), selected)
            self.assertEqual(select_host_toolchain(toolchains, str(selected.cc)), selected)

            with self.assertRaises(LLVMManagerError):
                select_host_toolchain(toolchains, "gcc")

    def test_ignores_unrelated_path_entry_that_cannot_be_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            clang = self._compiler(binary_dir, "clang", "clang version 18.1.8")
            (binary_dir / "clang++").symlink_to(clang.name)
            protected = binary_dir / "weakpass_edit"
            protected.write_text("not a compiler\n", encoding="utf-8")

            original_is_file = Path.is_file

            def guarded_is_file(candidate: Path) -> bool:
                if candidate.name == protected.name:
                    raise PermissionError(13, "Permission denied", str(candidate))
                return original_is_file(candidate)

            with mock.patch.object(Path, "is_file", guarded_is_file):
                toolchains = discover_host_toolchains(str(binary_dir))

            self.assertEqual(len(toolchains), 1)
            self.assertEqual(toolchains[0].family, "clang")

    def test_excludes_compiler_that_cannot_link_a_program(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary_dir = Path(temporary)
            clang = binary_dir / "clang"
            clang.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'clang version 18.1.8'\n",
                encoding="utf-8",
            )
            clang.chmod(0o755)
            (binary_dir / "clang++").symlink_to(clang.name)

            self.assertEqual(discover_host_toolchains(str(binary_dir)), [])


if __name__ == "__main__":
    unittest.main()
