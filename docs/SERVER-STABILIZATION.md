# Server stabilization acceptance

The general server refactor and approved dark Material 3 portal were deployed on
2026-11-04 at 15:32 UTC (21:02 IST). Runtime source is commit
`0cf104ab616c6efb940e5d316e3346f79a425c89`. The service-aware activation completed
in 5.35 seconds; this duration measures the command, not an outage measurement.

## Result

- Dashboard responsibilities now have separate owners for credentials, notes,
  conversion, service metadata, service reporting, role templates and AGY sessions.
- Credential changes take effect on the next lookup. Conversion aliases share the
  same admission limit. Memory reporting preserves unknown values instead of
  inventing measurements.
- Portal reads have a shared polling owner with cancellation, coalescing and
  bounded retries. In-flight notes and conversion writes survive tab visibility
  changes.
- Admin, family and login pages use the approved charcoal, teal and gold Material
  3 direction, with keyboard-accessible converter dialogs, visible focus, reduced
  motion and mobile touch targets.
- A complete, verified release now uses scoped service shutdown, journaled file
  replacement, health checks and service-aware recovery. Unknown file or disable
  marker changes stop activation, including an inode-preserving marker touch.
- The Android transfer gateway is built reproducibly from maintained Go source.
  Caddy's repeated route configuration shares one maintained definition.

## Deployment acceptance

The release contains 43 owned runtime files. All deployed hashes, owners and
canonical permissions matched the manifest after activation. The boot symlink's
raw target and inode remained intact. Its target content also matched.

Only dashboard, Caddy and transfer gateway restarted, alongside their supervisor.
SSH, FileBrowser, AdGuard, AGY, laptop terminal, Suwayomi, its GraphQL filter and
battery guard retained their process start identities. The supervisor reported
11 healthy services and four intentionally disabled services: Syncthing, terminal,
Tor and Vaultwarden. Those four markers retained their original metadata.

Portal `:8000`, dashboard `:8001`, gateway `:8081` health routes and Suwayomi
`:4567/` returned HTTP 200. Through the live portal:

- Unauthenticated admin API reads returned 401; family reads returned 403;
  administrator reads returned 200. The private polling script redirected an
  unauthenticated browser to login and denied family access.
- Login, admin and family HTML, shared CSS and the polling script matched their
  installed source bytes. Logout cleared both session cookie names.
- Existing notes remained readable without modifying them.
- A synthetic text upload through `/api/convert/pdf` produced a 14,150-byte PDF
  in 4.172 seconds, with a valid PDF header.
- A unique 66-byte synthetic file uploaded and downloaded through
  Caddy, gateway and FileBrowser with exact content equality. Cleanup returned
  204; a subsequent lookup returned 404. Existing files were not overwritten.

Credentials, tokens, notes and user file contents stayed on the phone during
these probes. Only synthetic payloads and sanitized results were used for
acceptance.

## Verification and measurement limits

The final Python suite passed 262 tests in 14.854 seconds, with two optional
network-dependent Caddy checks skipped. The actual Caddy adaptation comparison
had already passed separately against the phone binary. Six browser suites,
11 polling tests and the installer guard passed for the unchanged portal source.
These browser checks used isolated fixtures; they do not establish exhaustive
accessibility coverage or physical-device rendering quality.

Standalone recovery was tested using its copied command, real temporary service
processes, a deleted bundle and a broken candidate catalog. It stopped the
candidate, restored prior bytes and permissions, restarted the prior service and
passed an idempotent second run. The successful Android activation was exercised
on the phone. A deliberately interrupted phone deployment or reboot was not
performed.

Identical bounded dashboard workloads used 18 statistics reads and six service
reads, serially, with 0.1-second spacing and one warmup per endpoint:

| Metric | Before | After |
| --- | ---: | ---: |
| Statistics median | 13.700 ms | 11.962 ms |
| Services median | 196.652 ms | 197.309 ms |
| Dashboard RSS after workload | 33,488 KiB | 33,952 KiB |
| Request failures | 0 | 0 |

These small, time-separated samples support functional stability. They do not
establish a general speed or memory improvement. The main outcome is clearer
ownership, truthful reporting and safer changes.

## Recovery for this release

Keep this phone directory while rollback is needed:

```text
$HOME/.cache/mobile-server-candidate-0cf104ab-20261104T153045Z/transaction
```

For a known interrupted activation, use its copied service-aware command:

```sh
tx="$HOME/.cache/mobile-server-candidate-0cf104ab-20261104T153045Z/transaction"
python "$tx/recovery_lifecycle/release_activation.py" recover \
  --transaction-dir "$tx" --namespace-root /data/data/com.termux/files
```

An explicit backout of this completed release requires `rollback`, using the
same copied script and arguments. `recover` is not an instruction to undo a
successful release. See [release transactions](RELEASE-TRANSACTIONS.md) and
[installer usage](INSTALLER.md). Do not use file-only recovery while affected
candidate processes run, or delete journals to bypass a drift refusal.

## Suwayomi boundary

Suwayomi stayed running. Its intentional `libdns_fix.so` preload, PRoot working
directory, launcher, native library and captured C source were preserved.
Suwayomi source files selected no deployment changes. The existing native DNS
library was not rebuilt; its runtime DNS behavior, Java tuning, library behavior
and scheduled updates remain for the subsequent focused Suwayomi investigation.

The supervisor and boot hook remain configured for automatic service recovery.
Power loss, Android process termination, connectivity and hardware can still
interrupt availability; no uptime guarantee or unattended reboot test is implied.
