# LLVM Manager

A dependency-free Python tool for discovering, building, installing, and switching between LLVM/Clang toolchains built from release tags, branches, or exact commits.

## Requirements

LLVM Manager requires Python 3.10 or newer. Building LLVM additionally requires:

- Git
- CMake
- Ninja
- A working host C and C++ compiler capable of compiling, linking, and running native programs
- Enough disk space and memory for an LLVM build

Check the complete build environment before doing anything else:

```sh
python3 llvm_manager.py find-tools
```

The dependency check reports every missing requirement together, lists each validated compiler toolchain, and prints a platform-appropriate package-manager command. In an interactive terminal it then asks before running that command:

```text
llvm-manager can try to install the missing dependencies with:
  sudo apt-get update && sudo apt-get install -y cmake ninja-build build-essential
This may require elevated privileges and can prompt for your sudo password.
Run the dependency installation command now? [y/N]:
```

Nothing is installed unless the user answers `y` or `yes`. The command is executed as fixed argument lists rather than through a shell, and the complete dependency check runs again afterward. The build only continues if the recheck succeeds. On Windows, the prompt warns that installation may trigger UAC; on macOS, developer-tool or Homebrew installation may require finishing an external installer before rerunning the check.

For non-interactive use:

```sh
# Report only; never prompt.
python3 llvm_manager.py find-tools --no-install-prompt

# Explicitly authorize running the detected installer without another prompt.
python3 llvm_manager.py find-tools --install-missing
python3 llvm_manager.py build 22.1.8 --install-missing

# Machine-readable report; this never prompts.
python3 llvm_manager.py find-tools --json
```

The command exits with status 1 whenever the machine is still not ready to build LLVM. `--json` cannot be combined with `--install-missing`, so JSON output remains machine-readable.

Linux, macOS, and Windows are supported. Unix shells use a managed activation block in the active shell profile. Windows uses the current user's persistent environment variables.

## Start the interactive menu

```sh
python3 llvm_manager.py
```

The first screen is:

```text
LLVM Manager
1) Check for existing installs
2) Display the installed versions
3) Switch installed version
4) Build & install an LLVM source revision
5) Exit
```

By default, all manager data stays beside the script:

```text
llvm-manager/
├── source/llvm-project/
├── build/llvmorg-22.1.8-Release/
├── install/llvmorg-22.1.8/
└── current -> install/llvmorg-22.1.8/
```

These generated directories are not included in the archive.

## Commands

All behavior is exposed through the single top-level entry point:

```sh
python3 llvm_manager.py find-tools
python3 llvm_manager.py scan
python3 llvm_manager.py list
python3 llvm_manager.py tags
python3 llvm_manager.py switch 22
python3 llvm_manager.py build 22.1.8
python3 llvm_manager.py build --branch main
python3 llvm_manager.py build --commit 0123456789abcdef
```

Global options such as `--root` and `--repo-url` go before the subcommand:

```sh
python3 llvm_manager.py --root "$HOME/.local/llvm-manager" build 22.1.8
```

The implementation lives in the `llvm_mgr/` Python package; there is no separate wrapper-scripts directory.

## Build behavior

Before fetching tags or branches, cloning source, or configuring CMake, the build command runs the same dependency check as `find-tools`. When requirements are missing in an interactive terminal, it shows the exact installation command, warns about sudo/administrator elevation, and asks whether to run it. It then rechecks the environment and will not start source work while Git, CMake, Ninja, or a usable C/C++ compiler toolchain is still missing.

A default build uses:

- `clang`, `clang-tools-extra`, and `lld`
- `compiler-rt`
- `Release`
- Ninja
- The native LLVM target only
- `cmake --build ... --target install`

On macOS, LLVM Manager also configures Clang with `-DCLANG_USE_XCSELECT=ON`.
This enables the Darwin driver to discover the active Apple SDK and its libc++
headers without requiring callers to add `-isysroot` or a manual libc++ include
path to every compile command.

The default install prefix identifies the requested source revision:

- Release tag: `./install/llvmorg-22.1.8/`
- Branch: `./install/branch-main/` or `./install/branch-release-22.x/`
- Commit: `./install/commit-0123456789ab/`

The interactive flow first asks whether to build a release tag, remote branch, or exact commit, and then asks before using the default prefix. Branch names are selected from the remote branch list. Commit IDs must contain 7 to 40 hexadecimal characters.

Examples:

```sh
# Build the default native toolchain.
python3 llvm_manager.py build 22.1.8

# Build the current tip of the main branch.
python3 llvm_manager.py build --branch main

# Build a release branch.
python3 llvm_manager.py build --branch release/22.x

# Build an exact commit.
python3 llvm_manager.py build --commit 0123456789abcdef

# Build all LLVM backends with 12 parallel jobs.
python3 llvm_manager.py build 22.1.8 --targets all --jobs 12

# Build selected backends and switch to the result.
python3 llvm_manager.py build --branch main \
  --targets 'X86;AArch64;WebAssembly' \
  --switch

# Customize projects and runtimes.
python3 llvm_manager.py build 22.1.8 \
  --projects clang,clang-tools-extra,lld \
  --runtimes compiler-rt
```

After checking out the selected revision, the manager reads `LLVM_VERSION_MAJOR` from LLVM's monorepo-level `cmake/Modules/LLVMVersion.cmake`, with fallbacks for older or downstream layouts. It then creates major-version aliases for installed executables. For LLVM 22, examples include `clang-22`, `clang++-22`, `llvm-config-22`, and `ld.lld-22`. Unix uses relative symbolic links; Windows uses hard links when possible and copies as a fallback.

The installation is verified by running `clang-<major> --version`, compiling a
small C source file, and compiling a C++20 source file that includes
`<concepts>`. The C++ check catches missing SDK or standard-library header search
paths that a C-only verification would otherwise miss.

## Switching versions

```sh
python3 llvm_manager.py switch 22
python3 llvm_manager.py switch 22.1.8
python3 llvm_manager.py switch llvmorg-22.1.8
python3 llvm_manager.py switch /custom/prefix
```

On Bash, Zsh, and other POSIX shells, switching updates the `current` symbolic link, writes `activate.sh`, and adds one idempotent managed block to the active shell's profile. Fish gets `activate.fish`. The activation defines:

- `LLVM_HOME`
- `PATH`
- `CC`
- `CXX`

Open a new shell after switching, or source the profile printed by the command.

On Windows, switching updates the current user's persistent `Path`, `LLVM_HOME`, `CC`, and `CXX` values. Open a new terminal afterward.

## Source checkout safety

The manager uses one checkout at `source/llvm-project`. Before changing tags, branches, or commits, it checks for modified or untracked files and refuses to overwrite them. Commit, stash, or remove local changes before retrying.

Tag and branch selections are resolved to a full commit before checkout. Builds use detached HEAD mode so selecting a branch never modifies or creates a local branch. The installation metadata records the requested source kind/value and the exact resolved commit hash, making moving branch builds traceable.

## Compatibility note

The build logic targets LLVM's modern monorepo CMake layout. Current and reasonably recent release tags and branches use this layout. Very old historical tags may require version-specific CMake options or source-tree arrangements and can fail with a clear error rather than being modified destructively.

## Tests

```sh
python3 -m unittest discover -v
python3 tests/container_smoke_test.py
```

The smoke test creates a temporary local Git repository and a fake CMake installer. It exercises dependency reporting, compiler discovery and validation, tag fetching, cloning, revision checkout, configuration, installation, numbered aliases, compiler verification, version switching, shell-profile editing, and install rediscovery without attempting a multi-hour LLVM compilation.
