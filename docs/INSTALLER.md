# Reviewed Transactional Installer Entrypoint

This document describes the interface, invariants, and operational boundaries of `scripts/install.sh`.

## 1. Overview and Architecture

The legacy `scripts/install.sh` performed unjournaled, in-place file copies (`cp`, `install`), unverified Caddy reloads, and uncoordinated process restarts. This unsafe synchronization model has been replaced by:
1. **File Transaction Engine (`scripts/release_transaction.py`)**: Provides atomic file replacement, live precondition verification, write-intent journaling, directory fsync, and rollback attempts.
2. **Service Activation Adapter (`scripts/release_activation.py`)**: Manages service dependency graphs, computes affected services, quiesces targets under lock, applies file transactions, and restarts affected services (including Caddy) through normal supervisor lifecycle management.
3. **Thin Installer Entrypoint (`scripts/install.sh`)**: A minimal Bash argument parser that validates caller inputs and delegates directly to the reviewed Python activation adapter via `exec`.

```
[ User / Release Process ]
            |
            v
   scripts/install.sh
(Strict CLI parser & guard)
            |
            | exec (preserves argv & exit code)
            v
scripts/release_activation.py  <--->  scripts/release_transaction.py
  (Lifecycle & service restarts)         (Atomic files & journal)
```

> [!IMPORTANT]
> **Release Prerequisite**: Review the complete candidate and its actual gateway artifact, pin live preconditions, inspect the plan, and verify recovery before applying. The wrapper delegates to the maintained activation adapter; invoking `plan` does not activate a release.

## 2. Command-Line Interface (CLI)

The installer entrypoint strictly requires a first positional action: `plan` or `apply`.

### Plan Command (Dry Preflight)
```bash
scripts/install.sh plan --bundle PATH --preconditions FILE [--namespace-root PATH]
```
- Performs dry preflight computation of candidate release changes against caller-supplied preconditions to derive the proposed action and affected service set.
- Does not independently poll live service health, modify live files, or interrupt running daemons.

### Apply Command (Transactional Activation)
```bash
scripts/install.sh apply --bundle PATH --preconditions FILE --transaction-dir PATH [--namespace-root PATH]
```
- Executes transactional release application under namespace and activation locks.
- Stages files, captures backup snapshots, quiesces affected services, applies atomic replacements, and restarts affected services through standard supervisor lifecycle controls.

### Argument Specifications
- `--bundle PATH`: Path to the reviewed complete release bundle directory. Must include all candidate Python modules, static web assets, templates, and the compiled gateway binary (`.work/mobile-gateway`).
- `--preconditions FILE`: Path to the reviewed live preconditions JSON file. Specifies expected SHA-256 digests, byte lengths, file modes, and UIDs for existing destination paths, plus absence checks for new files.
- `--transaction-dir PATH`: Path to a dedicated, private (`0700`) transaction directory used for staging, backups, and journal states. Mandatory for `apply`; forbidden for `plan`.
- `--namespace-root PATH`: Optional path to live namespace root (defaults to live environment, e.g. `/data/data/com.termux/files`).

Path arguments reject option-like values (values beginning with `-`). Paths with leading dashes must be specified using `./` or absolute path notation.

## 3. Strict Safety Invariants

- **Fail-Fast Guard**: A zero-argument invocation immediately emits:
  `Direct source writes via legacy installer are disabled: reviewed transactional activation adapter is required.`
  and exits with code 1 before resolving script paths or executing any external binaries.
- **Fail-Closed on Missing Adapter**: The entrypoint resolves `scripts/release_activation.py` relative to its own script directory. If the adapter is absent, it halts fail-closed before invoking the Python interpreter.
- **No Direct Mutation**: The shell wrapper contains no file copy logic, staging commands, Caddy operations, or service process kill logic.
- **Strict Input Validation**:
  - Positional action must be `plan` or `apply`.
  - Duplicate flags (including `--namespace-root`) are rejected.
  - Missing mandatory flags, empty argument values, or option-like values (e.g. `--preconditions` passed as the value to `--bundle`) are rejected before path resolution.
  - `--transaction-dir` is strictly disallowed on `plan`.
  - Shell array expansion (`"${cmd[@]}"`) and exact quoting prevent word splitting, globbing, or shell metacharacter expansion.
  - Does not use `eval`, source caller input, or alter ambient `HOME` or `PREFIX`.

## 4. Service Lifecycle and Operational Boundaries

- **Brief Affected-Service Interruption**: Release activation restarts only services whose executable code or assets have been touched. Unaffected background daemons remain online. Caddy is restarted via supervisor lifecycle management when gateway or configuration assets change; the installer does not call `caddy reload`.
- **Preserved Operational Invariants**:
  - **SSH / Interactive Shells**: Active SSH and terminal sessions are never terminated.
  - **Disabled Service Intent**: Services marked disabled under `~/.config/services_disabled/` (e.g. Tor) remain disabled.
  - **Suwayomi**: Runs under an independent lifecycle with dedicated DNS resolution (`127.0.0.1:5335`) and is preserved across dashboard updates.
- **Honest Limitations**: The transaction engine and activation adapter govern filesystem files and service lifecycles. They do not manage external database migrations (e.g., SQLite databases for Filebrowser or Suwayomi). Database backups and schema changes must follow designated application data management procedures.

## 5. Recovery and Rollback

Recovery and rollback procedures are managed by the release activation adapter and file transaction engine rather than the wrapper:
- In case of failure during `apply`, automatic rollback is attempted, subject to guard, process, and drift verification. If unknown concurrent modifications, missing preimage files, or surviving processes are detected, rollback transitions to `RECOVERY_REQUIRED`, requiring manual or standalone intervention rather than promising unconditional restoration.
- For manual or interrupted recovery, refer to the activation adapter documentation in `docs/RELEASE-TRANSACTIONS.md` and `scripts/release_activation.py`.
- Recovery can be initiated using:
  ```bash
  python3 scripts/release_activation.py recover --transaction-dir PATH [--namespace-root PATH]
  ```
  or via the standalone recovery script saved directly into the transaction directory, which requires no candidate bundle or live module imports.
