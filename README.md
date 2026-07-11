# LLVM Manager

A dependency-free Python 3.10+ tool for discovering, building, installing, and switching between LLVM/Clang toolchains from release tags, branches, or exact commits.

## Installation and workspace location

Run directly from the checkout:

```sh
python3 llvm_manager.py --help
```

Or install the package:

```sh
python3 -m pip install .
llvm-manager --help
```

When run directly through `llvm_manager.py`, LLVM Manager keeps its workspace beside that script. The checkout therefore remains self-contained:

- `source/llvm-project` contains the LLVM Git clone.
- `build/` contains generated build trees.
- `install/` contains versioned installed toolchains.
- State, activation scripts, and the `current` link are also stored in the workspace root.

When invoked through an installed `llvm-manager` console command, the current working directory is the default workspace. Override either behavior with the global `--root` option or the `LLVM_MANAGER_ROOT` environment variable. Read-only commands such as `list` and `scan` do not create the workspace.

## Build requirements

Building LLVM requires Git, CMake, Ninja, and a host C/C++ compiler that can compile, link, and run native programs.

```sh
llvm-manager find-tools
```

The command reports every missing dependency and, when possible, shows a platform package-manager command. In an interactive terminal it asks before running that command. Automatic installation never uses a shell-composed command and is disabled when required elevation tooling is unavailable.

```sh
# Report only.
llvm-manager find-tools --no-install-prompt

# Explicitly authorize the detected installer.
llvm-manager find-tools --install-missing
llvm-manager build 22.1.8 --install-missing

# Machine-readable and never interactive.
llvm-manager find-tools --json
```

## Commands

```sh
llvm-manager scan
llvm-manager list
llvm-manager list --json
llvm-manager tags
llvm-manager tags --include-prerelease
llvm-manager switch 22
llvm-manager switch 22.1.8
llvm-manager switch '#2'
llvm-manager switch /custom/llvm/prefix
llvm-manager build 22.1.8
llvm-manager build --branch main
llvm-manager build --commit 0123456789abcdef
llvm-manager activate --shell fish
```

A bare number selects an LLVM major when that major exists. Use `#N` to unambiguously select list entry N.

External commands are echoed by default. Add the global `--quiet` option before the subcommand to suppress them:

```sh
llvm-manager --quiet list
```

Running without a subcommand opens the interactive menu when stdin is a terminal. Noninteractive builds must specify a release tag, `--branch`, or `--commit`.

## Build behavior

A default build enables:

- `clang`, `clang-tools-extra`, and `lld`
- `compiler-rt`
- `Release`
- Ninja
- The native LLVM target
- `cmake --build ... --target install`

On macOS, Clang is configured with `-DCLANG_USE_XCSELECT=ON` so the Darwin driver can discover the active Apple SDK and libc++ headers.

Examples:

```sh
llvm-manager build 22.1.8
llvm-manager build --branch release/22.x
llvm-manager build 22.1.8 --targets all --jobs 12
llvm-manager build --branch main --targets 'X86;AArch64;WebAssembly' --switch
llvm-manager build 22.1.8 --projects clang,clang-tools-extra,lld --runtimes compiler-rt
llvm-manager build 22.1.8 --install-dir /custom/llvm-22 --switch
```

The resolved Git commit, build configuration, selected host toolchain, and installed-file manifest are recorded in `.llvm-manager.json`. Build directories are automatically reset when their configuration changes. `--clean` explicitly clears the build directory and a manager-owned install before rebuilding; it refuses to recursively delete an unrecognized custom prefix.

CMake is given the exact discovered compiler and Ninja paths, avoiding a second inconsistent PATH lookup.

### Verification

After installation, LLVM Manager:

1. Creates and validates major-version aliases for known LLVM tools, such as `clang-22`, `clang++-22`, `llvm-config-22`, and `ld.lld-22`.
2. Runs `clang-<major> --version`.
3. Compiles and links a C executable.
4. Compiles and links a C++20 executable that includes `<concepts>`.
5. Runs both executables for native builds.

Use `--no-verify` only when the target cannot run on the host or verification must be handled separately.

## Switching versions

```sh
llvm-manager switch 22
llvm-manager switch llvmorg-22.1.8
llvm-manager switch /custom/prefix
```

On POSIX systems, switching atomically updates `current`, writes activation scripts, and maintains one managed block in the selected shell profile. Bash, Zsh, POSIX shells, and Fish use shell-specific rendering. Existing malformed or duplicate managed blocks are repaired.

On Windows, switching updates the current user's `Path`, `LLVM_HOME`, `CC`, and `CXX`. Only the exact previously active manager path is removed; unrelated PATH entries containing similar text are preserved.

The generated scripts are:

- `activate.sh`
- `activate.fish`
- `activate.ps1`

Show the appropriate script path with:

```sh
llvm-manager activate --shell bash
llvm-manager activate --shell fish
llvm-manager activate --shell pwsh
```

Switch state, profiles, symlinks, activation scripts, and Windows environment changes are guarded by a manager lock and rolled back when a later commit step fails.

## Source checkout safety

The manager uses one checkout at `source/llvm-project`. A manager-wide file lock prevents concurrent builds or switches from changing shared state underneath one another. Before changing revisions, the manager refuses to proceed when the checkout contains modified or untracked files.

Tags, branches, and commits are resolved to a full commit and checked out in detached-HEAD mode. Release-tag syntax is validated before dependency probing or repository work begins.

## Tests

```sh
python3 -m unittest discover -v
python3 tests/container_smoke_test.py
# or both:
python3 run_tests.py
```

The unit suite covers dependency planning, compiler discovery, install inspection, version selection, alias correction, build invalidation, shell rendering, transactional rollback, JSON/profile recovery, and CLI regressions. The smoke test exercises dependency discovery, tag and branch checkout, configure, install, alias creation, link/run verification, switching, and rediscovery. GitHub Actions runs the unit suite on Linux, macOS, and Windows with Python 3.10 and 3.13.
