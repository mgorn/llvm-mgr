# Test Results

## Package layout

The archive now uses one public launcher and one implementation package:

```text
llvm-manager/
├── llvm_manager.py
├── llvm_mgr/
├── tests/
├── pyproject.toml
├── README.md
└── run_tests.py
```

The former `scripts/` wrapper directory has been removed. Every operation is available through `llvm_manager.py` subcommands, including `find-tools`, `scan`, `list`, `tags`, `switch`, and `build`.

## Static validation

All Python sources compiled successfully with:

```sh
python3 -m compileall -q .
```

No stale references to the removed wrapper scripts remain in source code or documentation.

## Unit tests

Command:

```sh
python3 -m unittest discover -v
```

Result:

```text
Ran 20 tests
OK
```

Covered behavior includes:

- LLVM release-tag parsing and ordering
- Managed installation discovery
- Versioned executable aliases
- Git release-tag fetching and checkout
- Compiler-pair discovery and validation
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

The smoke test invokes the consolidated entry point directly:

```sh
python3 llvm_manager.py find-tools --json
```

It then validates this complete simulated flow:

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

The test uses a fake CMake installer and does not perform a resource-intensive full LLVM compilation.

## Platform limitation

These tests ran on a Linux host. Windows-specific behavior such as `vswhere`, `vcvarsall.bat`, registry environment updates, `.exe` alias handling, Winget, Chocolatey, and UAC interaction still requires a genuine Windows runner for complete validation.

## Packaging validation

The final project also built successfully as an installable Python wheel without build isolation:

```text
llvm_manager-1.4.0-py3-none-any.whl
```

The ZIP was extracted into a clean directory before rerunning all 20 unit tests and the end-to-end smoke test. The extracted layout contained only the top-level launcher, `llvm_mgr/` package, tests, and project metadata; no `scripts/` directory was present.
