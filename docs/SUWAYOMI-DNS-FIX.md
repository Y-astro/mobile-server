# Captured Suwayomi DNS Fix

The owner confirmed on 2026-11-04 that the live DNS preload fix and PRoot working-directory change were intentional. Maintained source now captures those existing changes exactly. This capture does not change or restart the phone.

## Source and installed artifact

| Maintained source | Installed path | SHA-256 | Bytes |
| --- | --- | --- | --- |
| [Launcher](../scripts/start_suwayomi.sh) | `$HOME/bin/start_suwayomi.sh` | `15754c27f3625f60b44918144875c87c18e989e83da4bf63b53ee5149be06e60` | 1869 |
| [Resolver source](../scripts/suwayomi/libdns_fix.c) | `$HOME/suwayomi/libdns_fix.c` | `902f39fb8c1f5a6a634de889e9981302af7653f48393021d01fac5b37316c777` | 7459 |
| No reproduced binary in this repository | `$HOME/suwayomi/libdns_fix.so` | `9e5dcc8e8f61be611556bf4a51f1b1c2b41e5bc5152bf571bc9427ea2633bc86` | 9936 |

These paths use the existing Termux namespace `/data/data/com.termux/files`, with `$HOME` ending in `/home` and UID `10505`. Observed modes were `0755` for the launcher and library, and `0644` for the C source. Launcher modification time was 2026-11-03 18:53:15 UTC; source and library timestamps preceded it by approximately one minute.

The launcher prepends the library to `LD_PRELOAD` and adds `--work-dir "$HOME/suwayomi"` to the Debian fallback invocation. The C source intercepts `getaddrinfo` and `freeaddrinfo`, tries configured UDP DNS upstreams, and falls back to the original resolver for some cases. Capturing this source does not establish correctness for every resolver, allocation, IPv6, or PRoot case.

## Release boundary

The original 35-row audit in [deployment-files.json](../config/deployment-files.json) remains immutable. Its launcher hash describes the earlier audit, before this intentional change. This document records an explicit reconciliation; deployment must not automatically accept any subsequent drift.

The general-server release must preserve the captured live launcher, library and their modes. It must not restart Suwayomi or reinstall an older launcher. The C source is tracked for ownership and later investigation; the manifest generator does not install it or the existing library. Verify the installed artifact separately before activation and refuse unexplained changes.

The library's compiler invocation, Android build reproducibility and runtime DNS behavior remain unverified. Host syntax checks do not verify the installed Android binary. Establish those facts during the deferred Suwayomi phase before changing or rebuilding this component.
