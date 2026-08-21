from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llvm_mgr.windows import (
    VisualStudioInstallation,
    augment_windows_search_path,
    visual_studio_llvm_prefixes,
)


class WindowsDiscoveryTests(unittest.TestCase):
    def test_explicit_search_path_is_not_augmented(self) -> None:
        with mock.patch("llvm_mgr.windows.os.name", "nt"):
            self.assertEqual(augment_windows_search_path("C:\\custom\\bin"), "C:\\custom\\bin")

    def test_default_search_path_combines_process_persistent_and_known_llvm_bins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            llvm_prefix = Path(temporary) / "LLVM"
            (llvm_prefix / "bin").mkdir(parents=True)
            with mock.patch("llvm_mgr.windows.os.name", "nt"), mock.patch(
                "llvm_mgr.windows.os.pathsep", ";"
            ), mock.patch.dict(
                "llvm_mgr.windows.os.environ",
                {"PATH": r"C:\process\bin;C:\shared\bin"},
                clear=True,
            ), mock.patch(
                "llvm_mgr.windows._persistent_windows_path_entries",
                return_value=[r"C:\persisted\bin", r"C:\shared\bin"],
            ), mock.patch(
                "llvm_mgr.windows.windows_llvm_prefixes",
                return_value=[llvm_prefix],
            ):
                result = augment_windows_search_path()

            entries = result.split(";")
            self.assertEqual(entries[:3], [r"C:\process\bin", r"C:\shared\bin", r"C:\persisted\bin"])
            self.assertEqual(entries[-1], str(llvm_prefix / "bin"))
            self.assertEqual(entries.count(r"C:\shared\bin"), 1)

    def test_visual_studio_llvm_prefixes_include_root_and_arch_specific_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary) / "Visual Studio"
            llvm_root = install / "VC" / "Tools" / "Llvm"
            (llvm_root / "bin").mkdir(parents=True)
            (llvm_root / "x64" / "bin").mkdir(parents=True)
            (llvm_root / "include").mkdir(parents=True)
            instance = VisualStudioInstallation(install, "Visual Studio Test", "17.0")

            with mock.patch("llvm_mgr.windows.os.name", "nt"), mock.patch(
                "llvm_mgr.windows.visual_studio_installations",
                return_value=[instance],
            ):
                prefixes = visual_studio_llvm_prefixes()

            self.assertEqual(prefixes, [llvm_root.resolve(), (llvm_root / "x64").resolve()])


if __name__ == "__main__":
    unittest.main()
