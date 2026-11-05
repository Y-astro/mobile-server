# Suwayomi lifecycle release

The lifecycle migration was deployed on 2026-11-05 at 06:17:32–06:17:42 UTC.
The runtime source is `f55a3e67c51dfa1e422348b689186c8ce04b6fd5`. Activation
completed in 10.621 seconds; its activation journal is `COMPLETED` and its file
transaction is `COMMITTED`.

## Runtime ownership

The supervisor now manages 16 services. It owns one native Suwayomi backend, the
existing GraphQL filter, and one foreground `suwayomi_scheduler` process. The
backend launcher no longer forks the filter or scheduler. Existing JVM arguments,
working directory, backend/filter/native ports, database paths, and nine dashboard
cards are preserved.

At acceptance, 12 services were healthy. The four existing disabled markers for
`agy`, `terminal`, `tor`, and `vaultwarden` retained their hashes, permissions,
ownership, device/inode identities, and timestamps. Syncthing had been enabled
before this deployment; the migration preserved its running processes.

The scheduler uses the existing singleton lock and midnight IST behavior. Its
startup did not trigger an update or download. The next scheduled run is
`2026-11-06 00:00:00` in the phone's local timezone. The last recorded successful
run remains `2026-11-04 00:00:00`; this migration did not backfill the missed
November 5 run or change catch-up policy.

## Acceptance evidence

- All 43 managed files match the candidate hashes, sizes, permissions and UID.
  Exactly three runtime files changed: the launcher, supervisor and catalog.
  The historical ownership manifest and boot alias retain their audited values.
- All 15 prior service configurations retain their order and complete values;
  only the foreground scheduler is appended. The default general and filter
  release profiles retain their restrictions.
- Only the dashboard, backend and supervisor changed process identities; the
  scheduler is new. All 13 unrelated prior service process sets, including SSH,
  Caddy, filter and Syncthing, remain identical.
- The previous backend and its two known zombie children are absent. The new
  backend has no zombie children. One scheduler holds its singleton lock and
  matches its recorded PID.
- JVM arguments and working directory are identical. The intentional DNS source
  and library hashes are unchanged, the DNS library remains first in preload
  order, and its mapping is present in the new JVM. The launcher also preserves
  the Termux exec preload inherited by the new process; selected environment
  hashes therefore differ from the old process. This is not a claim of a
  byte-identical environment or proof of every resolver path.
- Portal, dashboard, transfer gateway and native reader HTTP checks return 200.
  GraphQL reads through ports 4566, 4567 and 4569 return 200 with an empty,
  stopped download queue. A pre-existing cached thumbnail returns its matching
  bytes through the native route; the unauthenticated portal route returns 401.
- The installed customized upstream JAR is unchanged: SHA-256
  `9b7bf58329eccb3dcfb7246360f6818f7391b8804330a9e0491006049678bedc`.
  No custom downloader or page concurrency is deployed in this release.
  Later artifact inspection confirmed existing reader filtering, local WebUI
  validation and Android-native JNI customizations in this pinned input.

Before integration, 365 Python tests passed in 22.806 seconds, with two optional
Caddy checks skipped. Independent AGY Gemini 3.8 Flash High review approved the
source and reran the copied recovery proof plus 14 focused lifecycle tests. The
copied recovery proof uses temporary Linux processes; live Android JVM startup,
socket health and current service ownership were checked separately above.
A forced live rollback and a real midnight update were not used as acceptance tests.

## Retained release and recovery

The private phone staging directory is:

```text
/data/data/com.termux/files/home/.cache/mobile-server-suwayomi-lifecycle-f55a3e67-20261105T061441Z
```

It contains `bundle`, `release.tar.gz` and `transaction`. The transaction ID is
`act_9c0d71570a4f`. Archive SHA-256 is
`cf6aecaa6dfcc1d77775ab1cb72bde116d4697ba28c6e20607dbb1fa1bddd52a`.
Prior source preimages, recovery code and journals remain on the phone. Runtime
credentials, user databases, extensions, library and downloads are outside this
release's source ownership.

Use the retained activation adapter for an explicit software backout:

```sh
release_tx=/data/data/com.termux/files/home/.cache/mobile-server-suwayomi-lifecycle-f55a3e67-20261105T061441Z/transaction
"$PREFIX/bin/python3" "$release_tx/recovery_lifecycle/release_activation.py" rollback \
  --transaction-dir "$release_tx" --namespace-root /data/data/com.termux/files
```

`recover` resumes interrupted recovery; it does not undo a completed committed
release. Backout stops the candidate scheduler before restoring the prior catalog
and launches the prior native Java recipe directly. Running the old launcher
would respawn a scheduler that was absent before migration. Failed quiescence,
source integrity or health proof retains service guards and recovery-required
state rather than exposing a mixed release.

Local sanitized evidence is retained under the audit implementation worktree's
`.superpowers/sdd`: `suwayomi-lifecycle-package-record.json`,
`suwayomi-lifecycle-prestaging.json`, `suwayomi-lifecycle-staging.json`,
`suwayomi-lifecycle-native-preactivation.json`,
`suwayomi-lifecycle-activation.json` and `suwayomi-lifecycle-postactivation.json`.
