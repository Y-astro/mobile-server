# Suwayomi chapter parallelism release

Chapter parallelism is deployed on the Redmi Termux server. The release admits
up to two chapters per source, six chapters globally, and six downloader network
operations. Pages within each chapter remain serial. The existing six-source
setting, extension HTTP clients and rate limits remain in use.

Activation completed on 2026-11-05 at 13:57:31 UTC. It took 15.244 seconds and
restarted only the Suwayomi backend. The other 15 service process sets and the
supervisor stayed unchanged. Post-activation checks found 11 healthy services and
five intentionally disabled services: AGY, Syncthing, terminal, Tor and Vaultwarden.
Preserve the current disabled flags during any later operation; that list is an
observation, not a desired-state reset.

## Artifact and provenance

- Runtime source: `b1df662734003a9bcf8528e869fec0a9912ee5af`.
- Upstream: v2.4.2366, commit `40bc5da57820324ed0ae724cbbc020cc4673b594`.
- Installed JAR SHA-256: `e85adb62d6e65190984e1acc3d35d552402c9915067325744c0a9cd6ed461139`.
- Installed JAR size: 183,122,409 bytes; mode 0644; UID 10505.
- Prior customized JAR SHA-256: `9b7bf58329eccb3dcfb7246360f6818f7391b8804330a9e0491006049678bedc`.
- Compilation input SHA-256: `447b638b89256e762e06d79a1330b14600f3d85be4a8e71980980304bff6bec9`.
- Source patch SHA-256: `ad43e4e7f364c026664c59e87e4e2079b483773a1fb94f2840984b2e16d3a51c`.

The preservation packager overlays 50 classes from five explicit downloader
owners onto the pinned customized JAR. It preserves 83,720 unrelated ZIP entry
occurrences and their order, including duplicate dependency definitions. Eight
pinned custom entries match on the phone, including the custom reader assets and
Android Bionic zstd library. A complete upstream shadow JAR is a compilation input;
it is not the deployable phone artifact.

The build targets Java 21 using the pinned host toolchain described in
`suwayomi/parallel-downloads/README.md`. Run Gradle from the pinned upstream
checkout: upstream computes its Git revision from the process working directory.
The upstream build timestamp can change the full compilation input hash. Do not
substitute an old JAR solely because its filename contains the expected version.
Verify the input hash and required new classes, then repeat the package gates for
any rebuilt input.

## Correctness and resource evidence

The focused upstream gate passed 20 tests. The preserved-base packaged runtime
passed 21 tests, including 11 class-loading origin checks. Tests cover cancellation,
stop, dequeue/requeue, reorder/resume, retries, source admission, exact output,
and six-permit streaming pressure. Bypassing path exclusion while retaining its
reference accounting fails the real concurrent preparation/write regression.

Path ownership covers preparation through finalization. Distinct IDs that share
an existing sanitized output path cannot write concurrently. Cancellation releases
waiting references. The patch preserves existing filenames and completed alias
reuse; it does not migrate or rename existing downloads.

Sixteen synthetic paired trials exercised both CBZ and folder output with identical
logical payloads, private data/preferences/cache paths, production JVM flags, and
the configured `-Xmx384m` limit. The runtime heap ceiling was 402,587,648 bytes after
SerialGC rounding. All trials verified PNG bytes and SHA-256, page counts, and
ComicInfo series/title/number fields. Claims and streams settled to zero and all
six permits returned. File descriptors stayed at 274. Late settled heap samples
were about 43 MiB, with 30 threads for CBZ and 32 for folder trials.

Synthetic latency produced measured speedups of 1.76× for CBZ and 1.95× for folder
output. These are host fixture results, not Android or real-source throughput
promises. The short run does not establish absence of every possible leak. The
operator accepts temporary heating and battery impact; neither blocks this release.

Native phone checks verified all 44 owned file hashes, modes and UIDs; unchanged
JVM arguments, working directory, allowlisted environment hash and DNS preload
mapping; healthy HTTP on four ports and GraphQL on three ports; an empty stopped
download queue; and no old backend or backend zombie children. A pre-existing
cached thumbnail returned matching image bytes, while the unauthenticated portal
request returned 401. No user chapters were downloaded or deleted for validation.

## Retained release and rollback

Transaction `act_1605dfee3006` is `COMPLETED`; the file transaction is `COMMITTED`.
The phone retains the bundle, prior JAR preimage and verified copied recovery code:

```text
/data/data/com.termux/files/home/.cache/mobile-server-parallel-jar-b1df6627-20261105T134831Z
```

The archive SHA-256 is
`d4c04f6fed55545c473a44a1c65dc79719d1e154ff9c2bec94c9cc2c4cafe955`.
The release has 44 owned assets and 53 transport files. Its strict `suwayomi-jar`
profile permits only replacement of the existing JAR; launcher and 16-service
catalog pins must match. It refuses unexpected source drift or surviving JAR
readers before replacing or restoring the artifact.

For an intentional rollback, run on the phone:

```sh
release_tx="$HOME/.cache/mobile-server-parallel-jar-b1df6627-20261105T134831Z/transaction"
python "$release_tx/recovery_lifecycle/release_activation.py" rollback \
  --transaction-dir "$release_tx" \
  --namespace-root /data/data/com.termux/files
```

`recover` is idempotent transaction recovery; it does not undo a committed release.
Use explicit `rollback` to restore the prior JAR. The copied recovery test restored
the prior JAR after deleting the source bundle and preserved disabled-flag identity.
That destructive recovery test used an isolated Linux process model; the live phone
was not rolled backward merely to repeat it.

## Remaining work

Pages within a chapter remain serial. Page parallelism requires its own bounded
correctness and resource checks. Tsumiru read-triggered server-download cleanup is
a separate investigation: read status syncs, but the server copy remains. This
release changes no deletion policy and performs no retrospective cleanup.
