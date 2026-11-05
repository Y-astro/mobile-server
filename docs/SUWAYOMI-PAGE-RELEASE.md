# Suwayomi bounded page parallelism release

The Redmi server now downloads up to two chapters per source and two pages per chapter, with six active chapters and six downloader network operations globally. The configured six-source limit and extension rate limits remain in use. The dark Material 3 dashboard and other services remain deployed.

Activation finished at 2026-11-05T16:40:40.422795+00:00 and took 14.136 seconds. Only the Suwayomi backend restarted. All other 15 service process sets, the supervisor, JVM arguments, working directory, allowlisted environment, DNS shim and disabled flags matched their pre-activation snapshots. Post-activation checks found 12 healthy services and four intentionally disabled services: Syncthing, terminal, Tor and Vaultwarden. Preserve current flags rather than treating this observation as desired state.

## Artifact

- Runtime source: `46956d235df5f6b70b864fe4caf91f965e92d2a5`.
- Upstream: `40bc5da57820324ed0ae724cbbc020cc4673b594`, Java 21 target.
- Installed JAR SHA-256: `3dd150705d07486e4f890711a2d01a1a67a35d71a0fdc444cf7d5661473c221e`.
- Installed size: 183,131,587 bytes; mode 0644; UID 10505.
- Previous chapter-parallel JAR: `e85adb62d6e65190984e1acc3d35d552402c9915067325744c0a9cd6ed461139`.
- Source patch SHA-256: `28e8ee54bbc793599878c8d7ecb2727ba028dc5ebae28e5490f3f0eb6fa663a3`.
- Compilation input SHA-256: `3c8eaa18810977fef1ead7d782c522b30dd12889ea3db15c7d4cb16c0572a89e`.

The existing preservation packager overlays 53 classes from five downloader owners onto the pinned customized base. It retains 83,720 unrelated ZIP entry occurrences, their order and duplicate definitions. Eight custom entries match, including reader assets and the Android Bionic zstd library. A full upstream shadow JAR must never replace this customized artifact.

## Verification

The preserved-base packaged runtime passed 29 focused tests: four safe-path tests, one page test, 23 downloader tests, and a packaged class-origin check. The source patch and all source hashes match their pins. Page workers use structured cancellation; their progress collectors settle before ComicInfo or archive finalization. Existing chapter admission, path exclusion, retries, cached files and output names remain covered.

The minimal standalone packet passed 24 host trials and 24 final native phone trials. Both CBZ and folder output verified exact PNG bytes and hashes, page counts, ComicInfo fields, monotonic progress and quiescence. Claims and streams returned to zero and all six network permits returned after every trial. Eleven production/dependency classes loaded from the exact candidate JAR.

The final native matrix took 27,344 ms. Maximum settled heap was 31,324,432 bytes; maximum settled RSS was 185,992 KiB, file descriptors 13, and settled threads 24. The native Java 21 SerialGC reported a 389,283,840-byte effective heap ceiling with the unchanged `-Xmx384m` flag. Its survivor-space accounting differs from the host Java version.

Synthetic single-source runs improved by 1.21–1.58 times. Multisource cap-pressure results ranged from 0.94 to 1.06 times, where six network permits already limit concurrency. These short fixture results do not promise real-source throughput or long-term leak absence. Heating and battery impact were not acceptance gates, as requested.

Fourteen health samples during the final private probe found stable managed process identities and disabled flags. Minimum system available memory was 2,118,200 KiB. The probe used a private working directory, HOME, preferences, temporary files, Android compatibility roots and an in-memory H2 database. It started no HTTP backend and used no real source requests, user database or library downloads/deletes. The verifier rejects wrong artifact hashes before bootstrap; its negative test created no workspace files. Probe argv deliberately excludes the Suwayomi keyword because the supervisor's legacy Java matcher is broad.

Post-activation verification checked all 44 owned hashes, modes and UIDs, four HTTP endpoints and three GraphQL endpoints. The download queue was empty and stopped; scheduler state and next run remained unchanged. The old backend exited without zombie children. Transaction `act_c3af9090642e` is COMPLETED and its file transaction is COMMITTED.

## Recovery

The verified release and independent recovery code remain on the phone:

```text
/data/data/com.termux/files/home/.cache/mobile-server-page-jar-46956d23-20261105T163852Z
```

The host full release archive SHA-256 is `af1650fe250014d82b6569139bd22a114fd17058957c5d0736e7412eef25aca3`. The phone received the small control archive and reused the exact JAR already verified by the private native gate. All 53 staged file hashes and modes matched before activation. The strict activation profile allowed only the existing JAR replacement and refused live drift or surviving readers.

An intentional rollback restores the previous chapter-parallel JAR and restarts only Suwayomi:

```sh
release_tx="$HOME/.cache/mobile-server-page-jar-46956d23-20261105T163852Z/transaction"
python "$release_tx/recovery_lifecycle/release_activation.py" rollback \
  --transaction-dir "$release_tx" --namespace-root /data/data/com.termux/files
```

The retained copied recovery code and preimage support rollback independently of the staging bundle. A live rollback was not run because the new release passed acceptance. The prior [chapter release](SUWAYOMI-PARALLEL-RELEASE.md) retains its separate recovery chain.

## Remaining issue

Tsumiru's server-side deletion after reading remains under investigation. Read status sync is confirmed. Reader-exit behavior still needs confirmation; no broader read-triggered deletion hook or retrospective library cleanup was added.
