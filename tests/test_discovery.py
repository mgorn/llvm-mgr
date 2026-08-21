from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llvm_mgr.config import ManagerPaths
from llvm_mgr.discovery import scan_installs


class DiscoveryTests(unittest.TestCase):
    def _clang(self, prefix: Path, version: str) -> None:
        bin_dir = prefix / "bin"
        bin_dir.mkdir(parents=True)
        clang = bin_dir / "clang"
        clang.write_text(f'#!/bin/sh\necho "clang version {version}"\n', encoding="utf-8")
        clang.chmod(0o755)

    def test_detects_managed_install_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manager"
            home = Path(temporary) / "home"
            prefix = root / "install" / "llvmorg-22.1.8"
            self._clang(prefix, "22.1.8")
            (prefix / ".llvm-manager.json").write_text('{"tag":"llvmorg-22.1.8"}\n', encoding="utf-8")
            paths = ManagerPaths.create(root, home)

            with patch("llvm_mgr.discovery._common_prefixes", return_value=[]), patch.dict("os.environ", {"PATH": ""}):
                installs = scan_installs(paths)

            self.assertEqual(len(installs), 1)
            self.assertTrue(installs[0].managed)
            self.assertEqual(installs[0].version.display, "22.1.8")

    def test_external_scan_uses_augmented_windows_search_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manager"
            home = Path(temporary) / "home"
            prefix = Path(temporary) / "external-llvm"
            self._clang(prefix, "21.1.0")
            paths = ManagerPaths.create(root, home)

            with patch("llvm_mgr.discovery._common_prefixes", return_value=[]), patch(
                "llvm_mgr.discovery.augment_windows_search_path",
                return_value=str(prefix / "bin"),
            ) as augment:
                installs = scan_installs(paths)

            augment.assert_called_once_with(None)
            self.assertEqual(len(installs), 1)
            self.assertFalse(installs[0].managed)
            self.assertEqual(installs[0].prefix, prefix.resolve())

    def test_external_install_remains_visible_when_version_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manager"
            home = Path(temporary) / "home"
            prefix = Path(temporary) / "external-llvm"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            clang = bin_dir / "clang"
            clang.write_text("not executable compiler output\n", encoding="utf-8")
            clang.chmod(0o755)
            paths = ManagerPaths.create(root, home)

            with patch("llvm_mgr.discovery._common_prefixes", return_value=[prefix]), patch(
                "llvm_mgr.discovery._clang_version", return_value=None
            ):
                installs = scan_installs(paths, search_path="")

            self.assertEqual(len(installs), 1)
            self.assertEqual(installs[0].prefix, prefix.resolve())
            self.assertIsNone(installs[0].version)


if __name__ == "__main__":
    unittest.main()
