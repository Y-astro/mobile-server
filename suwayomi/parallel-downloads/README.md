# Suwayomi Bounded Parallel Downloads Slice

## 1. Upstream Pin & Provenance

- **Repository**: `https://github.com/Suwayomi/Suwayomi-Server.git`
- **Pinned Tag**: `v2.4.2366`
- **Pinned Commit**: `40bc5da57820324ed0ae724cbbc020cc4673b594`
- **Git Commit Count**: `2366` (`git rev-list HEAD --count`)
- **License**: Mozilla Public License 2.0 (MPL-2.0)

## 2. Toolchain Requirements

- **Host Java**: OpenJDK `27.0.1.0.3` (`/usr/lib/jvm/java-27-openjdk`)
- **Target JVM**: Java `21` (bytecode classfile major `65`)
- **Gradle Wrapper**: `9.7.1` (`gradle/wrapper/gradle-wrapper.properties`)
- **Kotlin Target**: `2.4.10`
- **AndroidCompat Pin**: `android-jar-1.0.0.jar` pinned to commit `3983289d9481a8ba99c0b16350b27f41364906a2` (SHA256: `2743be4f4c9ba50f8c86ccc0769e5be5246542488b34e6085a2574ca8ac303e3`). Included in private toolchain repo and prepended via `.work/.gradle-user-home/init.d/toolchain-pin.init.gradle.kts`.

## 3. Patch & Source Provenance

- **Patch file**: `suwayomi/parallel-downloads/bounded-chapters.patch`
  - SHA256: `5f8344403c051f30bcee77c9901b3ddf68b2bb8e5da548038159a086fc5a1e0f`
- **Patched upstream sources**:
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/DownloaderNetworkBudget.kt` (SHA256: `6c82adee19abbd331b97287f6cde1b48b0e132b16008160cc1f9734980971e18`)
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/Downloader.kt` (SHA256: `01f9a70da7ba39e1e7bbb8b08effc7d063b3479bcc8ccdb7c21b779aa8ebf89d`)
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/DownloadManager.kt` (SHA256: `e5ff37215e3ee26404fd2269448995b03798275cde5d5e74ba4c5f51697a2b83`)
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/fileProvider/ChaptersFilesProvider.kt` (SHA256: `f13f349d360140510a414ac9599b97d5da508f00e87655309a92c8685c8c35dc`)
  - `server/src/test/kotlin/suwayomi/tachidesk/manga/impl/download/BoundedParallelDownloaderTest.kt` (SHA256: `25bc697897799a9a20f92edf12bd3fd0fc515f38c1cd2c49b1ebdb2a0685115b`)

## 4. Reproducible Build Commands

```sh
# Apply patch to clean upstream v2.4.2366
git apply suwayomi/parallel-downloads/bounded-chapters.patch

# Build shadowJar artifact
ProductBuildType="Stable" \
GRADLE_USER_HOME=/home/astro/server-audit/2026-11-03/suwayomi-download-parallel/.work/.gradle-user-home \
./gradlew --no-daemon --max-workers=2 -Dorg.gradle.jvmargs="-Xmx2g" \
  :server:downloadWebUI :server:shadowJar
```

## 5. Verification Commands

```sh
ProductBuildType="Stable" \
GRADLE_USER_HOME=/home/astro/server-audit/2026-11-03/suwayomi-download-parallel/.work/.gradle-user-home \
./gradlew --no-daemon --max-workers=2 -Dorg.gradle.jvmargs="-Xmx2g" \
  :server:test --tests "suwayomi.tachidesk.SafePathTest" \
               --tests "suwayomi.tachidesk.manga.impl.PageTest" \
               --tests "suwayomi.tachidesk.manga.impl.download.BoundedParallelDownloaderTest"
```


## Root acceptance gate

The corrected implementation uses one identity-bound claim owner. Claims remain
held until cancellation settles; dequeue captures the original job instance.
The global chapter cap is six independently of the existing source-count setting;
a one-source setting can still run two chapters of that source. The network cap
covers full response-body streaming and closure. Page downloads remain serial.

Root added a gated ten-claim test to pressure the independent six-permit network
budget, corrected the fake response body's stable-source contract, and added the
one-source/two-chapter regression. The focused suite now contains 16 tests: four
SafePath, one PageTest and eleven downloader tests. It passed using the production
384 MiB test heap and JVM options in
`verification/production-test-heap.init.gradle.kts`. Compilation success and this
host test gate alone do not prove suitability for the phone.

`source-pin.json` records the current patch and source fingerprints. The complete
compiled shadow JAR is a compilation input, not the phone deployment artifact.
Package the reviewed class owners into the pinned installed customized base as
specified by the separately reviewed preservation packager. Include the new
`suwayomi/tachidesk/manga/impl/download/ChapterClaim` owner explicitly. Runtime
verification must load production classes from that packaged JAR, followed by
paired resource checks and the reviewed JAR-only activation profile.
