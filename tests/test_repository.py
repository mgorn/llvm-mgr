from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from llvm_mgr.repository import (
    LLVMRepository,
    RevisionKind,
    SourceRevision,
    fetch_release_tags,
    fetch_remote_branches,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


class RepositoryTests(unittest.TestCase):
    def test_fetch_and_checkout_tags_branches_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            origin = base / "origin"
            origin.mkdir()
            git(origin, "init")
            git(origin, "config", "user.email", "test@example.com")
            git(origin, "config", "user.name", "Test")
            (origin / "README").write_text("initial\n", encoding="utf-8")
            git(origin, "add", "README")
            git(origin, "commit", "-m", "initial")
            initial_commit = git(origin, "rev-parse", "HEAD")
            git(origin, "tag", "llvmorg-21.1.0")
            git(origin, "tag", "llvmorg-22.1.8")

            git(origin, "checkout", "-b", "feature/test")
            (origin / "README").write_text("feature\n", encoding="utf-8")
            git(origin, "commit", "-am", "feature")
            feature_commit = git(origin, "rev-parse", "HEAD")

            versions = fetch_release_tags(str(origin))
            self.assertEqual(versions[0].display, "22.1.8")
            self.assertIn("feature/test", fetch_remote_branches(str(origin)))

            checkout = base / "checkout"
            repository = LLVMRepository(checkout, str(origin))

            tag = repository.checkout(SourceRevision(RevisionKind.TAG, "llvmorg-21.1.0"))
            self.assertEqual(tag.commit, initial_commit)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), initial_commit)

            branch = repository.checkout(SourceRevision(RevisionKind.BRANCH, "feature/test"))
            self.assertEqual(branch.commit, feature_commit)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), feature_commit)

            commit = repository.checkout(SourceRevision(RevisionKind.COMMIT, initial_commit[:12]))
            self.assertEqual(commit.commit, initial_commit)
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), initial_commit)

    def test_source_revision_directory_names_and_commit_validation(self) -> None:
        self.assertEqual(
            SourceRevision(RevisionKind.TAG, "llvmorg-22.1.8").directory_name,
            "llvmorg-22.1.8",
        )
        self.assertEqual(
            SourceRevision(RevisionKind.BRANCH, "release/22.x").directory_name,
            "branch-release-22.x",
        )
        self.assertEqual(
            SourceRevision(RevisionKind.COMMIT, "0123456789abcdef").directory_name,
            "commit-0123456789ab",
        )
        with self.assertRaisesRegex(Exception, "hexadecimal"):
            SourceRevision(RevisionKind.COMMIT, "not-a-commit")
