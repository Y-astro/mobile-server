import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;

/** Ensures downloader tests exercise the packaged runtime and its dependencies. */
public class PackagedClassOriginTest {
    @Test
    public void productionClassesComeFromPackagedJar() throws Exception {
        Path expected = Path.of(System.getProperty("mobile.proof.jar")).toRealPath();
        List<String> names = List.of(
            "suwayomi.tachidesk.manga.impl.download.DownloadManager",
            "suwayomi.tachidesk.manga.impl.download.Downloader",
            "suwayomi.tachidesk.manga.impl.download.DownloaderNetworkBudget",
            "suwayomi.tachidesk.manga.impl.download.ChapterClaim",
            "suwayomi.tachidesk.manga.impl.download.fileProvider.ChaptersFilesProvider",
            "kotlin.Unit", "kotlinx.coroutines.Job", "okhttp3.OkHttpClient",
            "org.objectweb.asm.ClassReader", "com.sun.jna.Native",
            "org.jetbrains.exposed.v1.jdbc.Database"
        );
        for (String name : names) {
            Class<?> type = Class.forName(name, false, getClass().getClassLoader());
            Path actual = Path.of(type.getProtectionDomain().getCodeSource().getLocation().toURI()).toRealPath();
            assertEquals(expected, actual, name + " must load from the packaged runtime");
        }
        long heap = Runtime.getRuntime().maxMemory();
        assertTrue(heap <= 384L * 1024 * 1024, "Test JVM must retain the production heap cap");
        System.out.println("PACKAGED_ORIGIN_PROOF classes=" + names.size() + " maxHeapBytes=" + heap);
    }
}
