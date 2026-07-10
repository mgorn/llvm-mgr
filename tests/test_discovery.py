from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llvm_mgr.config import ManagerPaths
from llvm_mgr.discovery import scan_installs


class DiscoveryTests(unittest.TestCase):
    def test_detects_managed_install_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manager"
            home = Path(temporary) / "home"
            prefix = root / "install" / "llvmorg-22.1.8"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            clang = bin_dir / "clang"
            clang.write_text('#!/bin/sh\necho "clang version 22.1.8"\n', encoding="utf-8")
            clang.chmod(0o755)
            (prefix / ".llvm-manager.json").write_text('{"tag":"llvmorg-22.1.8"}\n', encoding="utf-8")
            paths = ManagerPaths.create(root, home)

            with patch("llvm_mgr.discovery._common_prefixes", return_value=[]), patch.dict("os.environ", {"PATH": ""}):
                installs = scan_installs(paths)

            self.assertEqual(len(installs), 1)
            self.assertTrue(installs[0].managed)
            self.assertEqual(installs[0].version.display, "22.1.8")
