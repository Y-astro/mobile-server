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
  - SHA256: `55f3353f62b113053340b55414e12dcc003a87b542e1144efae594b6ef4f2beb`
- **Patched upstream sources**:
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/DownloaderNetworkBudget.kt` (SHA256: `ada7e694b03bd4f5cc5d7e8afc88661f23086cc3084de62b1683bb2930f5c37f`)
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/Downloader.kt` (SHA256: `eb0240d7609b8dbae31a11d5ff087b8d57be9be04e18a740ff1c73ded4033c86`)
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/DownloadManager.kt` (SHA256: `42eed28a0564d5c9d4778e83cb3d584e820407e999d384b57d5fa0a14967bffd`)
  - `server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/fileProvider/ChaptersFilesProvider.kt` (SHA256: `f13f349d360140510a414ac9599b97d5da508f00e87655309a92c8685c8c35dc`)
  - `server/src/test/kotlin/suwayomi/tachidesk/manga/impl/download/BoundedParallelDownloaderTest.kt` (SHA256: `285e2761ceba08bff631e8936a420e5453d024478ab9ff5fa9dc4e993d81d29f`)

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
