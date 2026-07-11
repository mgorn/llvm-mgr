# Code-quality remediation summary

This update addresses the 40 findings from the code-quality review.

1. Package version now has one source of truth through setuptools dynamic metadata.
2. The default manager root is a writable per-user data directory with an environment override.
3. Discovery is read-only and no longer creates directories or writes an unused scan cache.
4. A newly built custom prefix is inspected directly before `--switch`; no unchecked `next()` remains.
5. Versioned `clang-N` candidates are ordered by numeric major.
6. Per-major tag selection can include prereleases.
7. Interactive reads use controlled error handling, and revision selection is rejected in noninteractive mode when omitted.
8. Version/major matching takes precedence over list indexing; `#N` is the explicit index syntax.
9. Prefix selection and general discovery share `inspect_install()`.
10. Install discovery requires an executable compiler and a successful version probe.
11. Installation verification links C and C++ programs and runs them for native builds.
12. Source-revision validation happens before dependency discovery and other expensive work.
13. Windows PATH switching removes only the exact previous and new manager `bin` paths.
14. Switching uses atomic writes, locking, rollback of profiles/symlinks/environment, and activation-script restoration.
15. Existing aliases are verified and corrected when stale.
16. Git, CMake, and Ninja are resolved once for a build; Ninja is passed to CMake explicitly.
17. Elevation state now distinguishes root, usable sudo, and unavailable sudo.
18. Linux package-manager behavior is data-driven rather than repeated branches.
19. One search PATH is threaded through dependency and installer detection.
20. Process probing and numeric-version extraction are shared utilities.
21. Command rendering is platform-aware, credentials are redacted, and `--quiet` disables command echoing.
22. JSON reads support strict errors or visible recovery warnings.
23. JSON and text state use unique, flushed, atomic temporary files.
24. Builds and switches share a cross-platform manager lock.
25. Install discovery reads the structured `source` metadata, with legacy-tag compatibility only.
26. The unused scan cache was removed.
27. Generated activation scripts are exposed through the `activate` command and Windows switch output.
28. Aliases are limited to recognized LLVM tools.
29. Fish, PowerShell, and POSIX activation rendering are separated; Bash profile selection handles existing login profiles.
30. External prefix discovery is normalized, deduplicated, and uses version-agnostic directory globbing.
31. CLI dispatch is split into focused command handlers.
32. Build configuration, cleanup, verification, dependency planning, and presentation were extracted into smaller units.
33. Terminal rendering lives in `presentation.py`; domain discovery returns structured data.
34. `scan` and `list` share one implementation, and the interactive menu no longer performs two equivalent scan actions.
35. Unused dependency fields, redundant alias conditions, and the unreachable command fallback were removed.
36. `run_tests.py` now has a guarded `main()`.
37. Build configuration changes invalidate stale build trees; managed install manifests support stale-file cleanup; `--clean` is safe for manager-owned prefixes.
38. Duplicate and interrupted shell-profile blocks are repaired into one canonical block.
39. Tag parsing and normalization share one grammar, including RC and `git` suffixes.
40. The suite now contains 44 unit tests plus the smoke test, and GitHub Actions covers Linux, macOS, and Windows on Python 3.10 and 3.13.
