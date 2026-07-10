from __future__ import annotations

import unittest

from llvm_mgr.versioning import LLVMVersion, latest_per_major, normalize_tag, parse_llvm_tag, parse_version_text


class VersioningTests(unittest.TestCase):
    def test_parse_stable_tag(self) -> None:
        version = parse_llvm_tag("llvmorg-22.1.8")
        self.assertEqual(version, LLVMVersion(22, 1, 8))
        self.assertTrue(version and version.stable)

    def test_parse_release_candidate(self) -> None:
        version = parse_llvm_tag("llvmorg-23.0.0-rc2")
        self.assertEqual(version, LLVMVersion(23, 0, 0, "rc2"))
        self.assertLess(version, LLVMVersion(23, 0, 0))

    def test_parse_clang_version_output(self) -> None:
        version = parse_version_text("clang version 21.1.7 (vendor build)")
        self.assertEqual(version, LLVMVersion(21, 1, 7))

    def test_latest_per_major_excludes_prereleases(self) -> None:
        versions = [
            LLVMVersion(22, 1, 7),
            LLVMVersion(22, 1, 8),
            LLVMVersion(23, 0, 0, "rc1"),
            LLVMVersion(21, 1, 4),
        ]
        self.assertEqual(latest_per_major(versions), [LLVMVersion(22, 1, 8), LLVMVersion(21, 1, 4)])

    def test_normalize_tag(self) -> None:
        self.assertEqual(normalize_tag("22.1.8"), "llvmorg-22.1.8")
        self.assertEqual(normalize_tag("llvmorg-22.1.8"), "llvmorg-22.1.8")
