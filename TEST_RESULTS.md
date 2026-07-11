# Test Results

## Package layout

The archive uses one public launcher and one implementation package:

```text
llvm-manager/
├── llvm_manager.py
├── llvm_mgr/
├── tests/
├── pyproject.toml
├── README.md
└── run_tests.py
```

Every operation is available through `llvm_manager.py` subcommands, including `find-tools`, `scan`, `list`, `tags`, `switch`, and `build`.

## Source revision support

Builds now accept three source kinds:

```sh
python3 llvm_manager.py build 22.1.8
python3 llvm_manager.py build --branch main
python3 llvm_manager.py build --commit 0123456789abcdef
```

The repository layer resolves tags, remote branches, and abbreviated or full commit IDs to a full commit hash and checks out that commit in detached HEAD mode. Branch and commit builds determine the executable major suffix from the monorepo-level `cmake/Modules/LLVMVersion.cmake`, with compatibility fallbacks for older or downstream layouts. Installation metadata records the requested source kind/value and resolved commit.

## Static validation

All Python sources compiled successfully with:

```sh
python3 -m compileall -q .
```

## Unit tests

Command:

```sh
python3 -m unittest discover -v
```

Result:

```text
Ran 25 tests
OK
```

Covered behavior includes:

- LLVM release-tag parsing and ordering
- Managed installation discovery
- Versioned executable aliases
- Git release-tag and remote-branch fetching
- Detached checkout by tag, branch, and exact commit
- Source-revision naming and commit-ID validation
- Compiler-pair discovery and validation
- Safe PATH scanning that ignores protected non-compiler entries
- Dependency reporting and installation prompting
- Dependency installation command sequencing without `shell=True`
- Build blocking before source work when requirements are missing
- Version switching and idempotent shell-profile updates

## End-to-end smoke test

Command:

```sh
python3 tests/container_smoke_test.py
```

Result:

```text
Container smoke test passed
```

The smoke test validates this complete simulated flow:

1. Dependency discovery
2. Host C/C++ compiler validation and selection
3. LLVM release-tag fetching from a temporary local Git repository
4. Source clone and detached tag checkout
5. CMake configuration with the selected C and C++ compilers
6. Simulated installation
7. Major-version executable alias creation
8. Installed compiler verification
9. Active-version switching
10. Shell-profile modification
11. Installed-version rediscovery
12. A second build from a named remote branch
13. Branch source metadata and resolved commit recording

The test uses a fake CMake installer and does not perform a resource-intensive full LLVM compilation.

## LLVM main-branch version detection regression

The branch/commit build path now follows LLVM's current monorepo layout and reads the primary version definition from:

```text
cmake/Modules/LLVMVersion.cmake
```

Focused tests cover the current root-level module, the older `llvm/CMakeLists.txt` fallback, and the diagnostic emitted when none of the supported locations contains `LLVM_VERSION_MAJOR`. The end-to-end fake repository now mirrors the current root-level CMake module layout, preventing this regression from being hidden by an unrealistic fixture.

## Platform limitation

These tests ran on a Linux host. Windows-specific behavior such as `vswhere`, `vcvarsall.bat`, registry environment updates, `.exe` alias handling, Winget, Chocolatey, and UAC interaction still requires a genuine Windows runner for complete validation.

## Packaging validation

The project built successfully as an installable Python wheel without build isolation:

```text
llvm_manager-1.5.1-py3-none-any.whl
```

The ZIP is extracted into a clean directory before the full unit and smoke-test suite is rerun.
