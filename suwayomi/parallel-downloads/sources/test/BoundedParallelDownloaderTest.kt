package suwayomi.tachidesk.manga.impl.download

/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import eu.kanade.tachiyomi.source.model.FilterList
import eu.kanade.tachiyomi.source.model.MangasPage
import eu.kanade.tachiyomi.source.model.Page as SPage
import eu.kanade.tachiyomi.source.model.SChapter
import eu.kanade.tachiyomi.source.model.SManga
import eu.kanade.tachiyomi.source.online.HttpSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonObject
import okhttp3.Interceptor
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody
import okio.BufferedSource
import okio.buffer
import okio.source
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.jdbc.insertAndGetId
import org.jetbrains.exposed.v1.jdbc.select
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import org.jetbrains.exposed.v1.jdbc.update
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import suwayomi.tachidesk.manga.impl.Page
import suwayomi.tachidesk.manga.impl.download.fileProvider.ChaptersFilesProvider
import suwayomi.tachidesk.manga.impl.download.model.DownloadState
import suwayomi.tachidesk.manga.impl.download.model.Status
import suwayomi.tachidesk.manga.impl.util.getChapterCachePath
import suwayomi.tachidesk.manga.impl.util.getChapterCbzPath
import suwayomi.tachidesk.manga.impl.util.getChapterDownloadPath
import suwayomi.tachidesk.manga.impl.util.lang.EMPTY
import suwayomi.tachidesk.manga.impl.util.source.GetSource
import suwayomi.tachidesk.manga.model.table.ChapterTable
import suwayomi.tachidesk.manga.model.table.MangaTable
import suwayomi.tachidesk.manga.model.table.PageTable
import suwayomi.tachidesk.server.ApplicationDirs
import suwayomi.tachidesk.server.serverConfig
import suwayomi.tachidesk.test.ApplicationTest
import suwayomi.tachidesk.test.clearTables
import uy.kohesive.injekt.Injekt
import uy.kohesive.injekt.api.get
import java.awt.image.BufferedImage
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.zip.ZipFile
import javax.imageio.ImageIO
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class BoundedParallelDownloaderTest : ApplicationTest() {

    private val registeredSourceIds = mutableListOf<Long>()
    private val globalActiveStreams = AtomicInteger(0)
    private val globalPeakStreams = AtomicInteger(0)

    companion object {
        fun createTestPngBytes(chapterId: Int, pageNum: Int): ByteArray {
            val img = BufferedImage(2, 2, BufferedImage.TYPE_INT_ARGB)
            val color = ((chapterId and 0xFF) shl 16) or (((pageNum + 1) and 0xFF) shl 8) or 0x55 or (0xFF shl 24)
            img.setRGB(0, 0, color)
            val baos = ByteArrayOutputStream()
            ImageIO.write(img, "png", baos)
            return baos.toByteArray()
        }
    }

    class GatedStreamingResponseBody(
        private val mediaType: MediaType,
        private val bytes: ByteArray,
        private val onStreamOpen: () -> Unit,
        private val onStreamClose: () -> Unit,
        private val readDelayMs: Long = 0,
    ) : ResponseBody() {
        override fun contentType(): MediaType = mediaType
        override fun contentLength(): Long = bytes.size.toLong()

        override fun source(): BufferedSource = bufferedSource

        private val bufferedSource: BufferedSource by lazy {
            val bais = ByteArrayInputStream(bytes)
            val stream = object : InputStream() {
                private var opened = false

                override fun read(): Int {
                    if (!opened) {
                        opened = true
                        onStreamOpen()
                    }
                    if (readDelayMs > 0) {
                        Thread.sleep(readDelayMs)
                    }
                    return bais.read()
                }

                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    if (!opened) {
                        opened = true
                        onStreamOpen()
                    }
                    if (readDelayMs > 0) {
                        Thread.sleep(readDelayMs)
                    }
                    return bais.read(b, off, len)
                }

                override fun close() {
                    try {
                        bais.close()
                    } finally {
                        if (opened) {
                            opened = false
                            onStreamClose()
                        }
                    }
                }
            }
            stream.source().buffer()
        }
    }

    inner class FakeHttpSource(
        override val id: Long,
        override val name: String = "FakeSource-$id",
        override val lang: String = "en",
        override val baseUrl: String = "https://fake-$id.local",
        val pagesPerChapter: Int = 2,
        val pageStreamDelayMs: Long = 40,
        val failPages: Boolean = false,
        var onPageStreamStarted: ((chapterId: Int, pageNum: Int) -> Unit)? = null,
        var onPageStreamClosed: ((chapterId: Int, pageNum: Int) -> Unit)? = null,
        var onGetPageListStarted: ((chapterId: Int) -> Unit)? = null,
        var payloadProvider: ((chapterId: Int, pageNum: Int) -> ByteArray)? = null,
        var pageInterceptor: ((chapterId: Int, pageNum: Int) -> Response?)? = null,
    ) : HttpSource() {
        override val supportsLatest: Boolean = false

        val activeStreams = AtomicInteger(0)
        val peakStreams = AtomicInteger(0)

        private val interceptor = Interceptor { chain ->
            val request = chain.request()
            if (failPages) {
                throw IOException("Simulated network failure for ${request.url}")
            }

            val pathSegments = request.url.pathSegments
            val chapterId = pathSegments.getOrNull(2)?.toIntOrNull() ?: 1
            val pageNum = pathSegments.getOrNull(4)?.substringBefore(".")?.toIntOrNull() ?: 0

            val customResponse = pageInterceptor?.invoke(chapterId, pageNum)
            if (customResponse != null) {
                return@Interceptor customResponse
            }

            val bytes = payloadProvider?.invoke(chapterId, pageNum) ?: createTestPngBytes(chapterId, pageNum)
            val body = GatedStreamingResponseBody(
                mediaType = "image/png".toMediaType(),
                bytes = bytes,
                onStreamOpen = {
                    val s = activeStreams.incrementAndGet()
                    peakStreams.updateAndGet { peak -> if (s > peak) s else peak }
                    val g = globalActiveStreams.incrementAndGet()
                    globalPeakStreams.updateAndGet { peak -> if (g > peak) g else peak }
                    onPageStreamStarted?.invoke(chapterId, pageNum)
                },
                onStreamClose = {
                    activeStreams.decrementAndGet()
                    globalActiveStreams.decrementAndGet()
                    onPageStreamClosed?.invoke(chapterId, pageNum)
                },
                readDelayMs = pageStreamDelayMs,
            )

            Response.Builder()
                .request(request)
                .protocol(Protocol.HTTP_1_1)
                .code(200)
                .message("OK")
                .body(body)
                .build()
        }

        override val client: OkHttpClient = OkHttpClient.Builder()
            .addInterceptor(interceptor)
            .build()

        override suspend fun getPageList(chapter: SChapter): List<SPage> {
            val chapterId = chapter.url.substringAfterLast("/").toIntOrNull() ?: 0
            onGetPageListStarted?.invoke(chapterId)
            return (0 until pagesPerChapter).map { i ->
                SPage(i, "$baseUrl${chapter.url}/page/$i", "$baseUrl${chapter.url}/page/$i.png")
            }
        }

        override fun popularMangaRequest(page: Int): Request = throw UnsupportedOperationException()
        override fun popularMangaParse(response: Response): MangasPage = throw UnsupportedOperationException()
        override fun latestUpdatesRequest(page: Int): Request = throw UnsupportedOperationException()
        override fun latestUpdatesParse(response: Response): MangasPage = throw UnsupportedOperationException()
        override fun searchMangaRequest(page: Int, query: String, filters: FilterList): Request = throw UnsupportedOperationException()
        override fun searchMangaParse(response: Response): MangasPage = throw UnsupportedOperationException()
        override fun mangaDetailsParse(response: Response): SManga = throw UnsupportedOperationException()
        override fun chapterListParse(response: Response): List<SChapter> = throw UnsupportedOperationException()
        override fun pageListParse(response: Response): List<SPage> = throw UnsupportedOperationException()
        override fun imageUrlParse(response: Response): String = throw UnsupportedOperationException()
    }

    private fun registerFakeSource(source: FakeHttpSource) {
        GetSource.registerSource(source.id to source)
        registeredSourceIds.add(source.id)
    }

    private fun createManga(title: String, sourceId: Long): Int =
        transaction {
            MangaTable.insertAndGetId {
                it[MangaTable.title] = title
                it[MangaTable.url] = "/manga/$title"
                it[MangaTable.sourceReference] = sourceId
                it[MangaTable.inLibrary] = true
            }.value
        }

    private fun createChapter(mangaId: Int, index: Int, name: String = "Chapter $index"): Int =
        transaction {
            val chId =
                ChapterTable.insertAndGetId {
                    it[ChapterTable.url] = ""
                    it[ChapterTable.name] = name
                    it[ChapterTable.sourceOrder] = index
                    it[ChapterTable.isRead] = false
                    it[ChapterTable.manga] = mangaId
                    it[ChapterTable.memo] = JsonObject.EMPTY
                    it[ChapterTable.isDownloaded] = false
                    it[ChapterTable.pageCount] = 0
                }.value
            ChapterTable.update({ ChapterTable.id eq chId }) {
                it[ChapterTable.url] = "/chapter/$mangaId/$chId"
            }
            chId
        }

    private fun isChapterDownloaded(chapterId: Int): Boolean =
        transaction {
            ChapterTable
                .select(ChapterTable.isDownloaded)
                .where { ChapterTable.id eq chapterId }
                .firstOrNull()
                ?.get(ChapterTable.isDownloaded) ?: false
        }

    private suspend fun verifyChapterOutput(mangaId: Int, chapterId: Int, expectedPages: Int) {
        val cbzFile = File(getChapterCbzPath(mangaId, chapterId))
        val dir = File(getChapterDownloadPath(mangaId, chapterId))

        if (cbzFile.exists()) {
            ZipFile(cbzFile).use { zip ->
                val entries = zip.entries().asSequence().map { it.name }.toSet()
                assertTrue(entries.contains("ComicInfo.xml"), "CBZ must contain ComicInfo.xml")
                for (p in 0 until expectedPages) {
                    val pageName = "${Page.getPageName(p, expectedPages)}.png"
                    assertTrue(entries.contains(pageName), "CBZ missing page $pageName")
                    val entry = zip.getEntry(pageName)!!
                    val actualBytes = zip.getInputStream(entry).use { it.readBytes() }
                    val expectedBytes = createTestPngBytes(chapterId, p)
                    assertContentEquals(expectedBytes, actualBytes, "Page $p bytes mismatch in CBZ $cbzFile")
                }
                val pageFiles = entries.filter { it.endsWith(".png") }
                assertEquals(expectedPages, pageFiles.size, "Unexpected number of PNG files in CBZ")
            }
        } else if (dir.exists()) {
            val files = dir.listFiles()?.toList().orEmpty()
            val fileNames = files.map { it.name }.toSet()
            assertTrue(fileNames.contains("ComicInfo.xml"), "Directory must contain ComicInfo.xml")
            for (p in 0 until expectedPages) {
                val pageName = "${Page.getPageName(p, expectedPages)}.png"
                assertTrue(fileNames.contains(pageName), "Directory missing page $pageName")
                val file = File(dir, pageName)
                val actualBytes = file.readBytes()
                val expectedBytes = createTestPngBytes(chapterId, p)
                assertContentEquals(expectedBytes, actualBytes, "Page $p bytes mismatch in dir $dir")
            }
            val pageFiles = fileNames.filter { it.endsWith(".png") }
            assertEquals(expectedPages, pageFiles.size, "Unexpected number of PNG files in dir")
        } else {
            throw AssertionError("Neither CBZ nor directory exists for manga $mangaId chapter $chapterId")
        }
    }

    @BeforeEach
    fun setUp() {
        runBlocking {
            DownloadManager.clear()
        }
        clearTables(PageTable, ChapterTable, MangaTable)
        val appDirs = Injekt.get<ApplicationDirs>()
        File(appDirs.downloadsRoot).deleteRecursively()
        File(appDirs.downloadsRoot).mkdirs()
        File(appDirs.tempMangaCacheRoot).deleteRecursively()
        File(appDirs.tempMangaCacheRoot).mkdirs()
        DownloadManager.maxChaptersPerSourceOverride = null
        DownloadManager.maxGlobalActiveOverride = null
        ChaptersFilesProvider.pageWorkersOverride = 1
        serverConfig.maxSourcesInParallel.value = 6
        globalActiveStreams.set(0)
        globalPeakStreams.set(0)
        DownloaderNetworkBudget.resetForTest()
        Downloader.resetPathLocksForTest()
    }

    @AfterEach
    fun tearDown() {
        runBlocking {
            DownloadManager.clear()
        }
        for (id in registeredSourceIds) {
            GetSource.unregisterSource(id)
        }
        registeredSourceIds.clear()
        clearTables(PageTable, ChapterTable, MangaTable)
        val appDirs = Injekt.get<ApplicationDirs>()
        File(appDirs.downloadsRoot).deleteRecursively()
        File(appDirs.downloadsRoot).mkdirs()
        File(appDirs.tempMangaCacheRoot).deleteRecursively()
        File(appDirs.tempMangaCacheRoot).mkdirs()
        DownloadManager.maxChaptersPerSourceOverride = null
        DownloadManager.maxGlobalActiveOverride = null
        ChaptersFilesProvider.resetForTest()
        serverConfig.maxSourcesInParallel.value = 6
        DownloaderNetworkBudget.resetForTest()
        Downloader.resetPathLocksForTest()
    }

    @Test
    fun testSameSourceTwoChaptersOverlapAndComplete() = runBlocking {
        val sourceId = 9101L
        val mangaId = createManga("SameSourceManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")
        val ch2 = createChapter(mangaId, 2, "Ch 2")

        val ch1Streaming = AtomicBoolean(false)
        val ch2Streaming = AtomicBoolean(false)
        val overlapAchieved = CountDownLatch(1)
        val releaseStreams = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 2,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { chapterId, pageNum ->
                if (pageNum == 0) {
                    if (chapterId == ch1) {
                        ch1Streaming.set(true)
                    } else if (chapterId == ch2) {
                        ch2Streaming.set(true)
                    }
                    if (ch1Streaming.get() && ch2Streaming.get()) {
                        overlapAchieved.countDown()
                    }
                    releaseStreams.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2)))

        // Wait until both response body streams are verified to be open simultaneously
        val overlapped = overlapAchieved.await(10, TimeUnit.SECONDS)
        assertTrue(overlapped, "Chapters 1 and 2 must overlap active response body streaming")
        assertEquals(2, fakeSource.activeStreams.get(), "Both response bodies must be streaming concurrently")
        assertEquals(2, fakeSource.peakStreams.get(), "Peak same-source response body streams must be 2")

        // Release the gated streams
        releaseStreams.countDown()

        // Wait for both chapters to finish
        withTimeout(15000) {
            while (true) {
                if (isChapterDownloaded(ch1) && isChapterDownloaded(ch2) && (DownloadManager.getStatus().queue.isEmpty() && DownloadManager.activeClaims.isEmpty())) {
                    break
                }
                delay(40)
            }
        }

        // Output integrity: exact page names, exact page count, exact per-page bytes
        verifyChapterOutput(mangaId, ch1, 2)
        verifyChapterOutput(mangaId, ch2, 2)

        assertEquals(2, fakeSource.peakStreams.get(), "Peak active same-source streams must equal 2")
        assertEquals(0, fakeSource.activeStreams.get(), "All streams must be closed")
        assertTrue(DownloaderNetworkBudget.peakActiveUnits.get() <= 6, "Peak network permits must not exceed 6")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All network permits must be released")
    }

    @Test
    fun testCrossSourceConcurrencyAndGlobalLimit() = runBlocking {
        val numSources = 4
        val chaptersPerSource = 2
        val sources = (1..numSources).map { i ->
            val sourceId = 9200L + i
            val source = FakeHttpSource(id = sourceId, pagesPerChapter = chaptersPerSource, pageStreamDelayMs = 50)
            registerFakeSource(source)
            source
        }

        val allChapterIds = mutableListOf<Int>()
        val mangaChapterMap = mutableListOf<Pair<Int, Int>>()

        for (source in sources) {
            val mangaId = createManga("Manga-${source.id}", source.id)
            for (c in 1..chaptersPerSource) {
                val chId = createChapter(mangaId, c, "Ch $c")
                allChapterIds.add(chId)
                mangaChapterMap.add(mangaId to chId)
            }
        }

        DownloadManager.enqueue(DownloadManager.EnqueueInput(allChapterIds))

        withTimeout(25000) {
            while (true) {
                val allDone = allChapterIds.all { isChapterDownloaded(it) }
                if (allDone && (DownloadManager.getStatus().queue.isEmpty() && DownloadManager.activeClaims.isEmpty())) {
                    break
                }
                delay(50)
            }
        }

        // Verify bounds: global peak <= 6, per-source peak <= 2
        assertTrue(globalPeakStreams.get() <= 6, "Global active response body streams must not exceed 6")
        assertTrue(globalPeakStreams.get() >= 3, "Global active response body streams must reach at least 3 across sources")
        for (source in sources) {
            assertTrue(source.peakStreams.get() <= 2, "Per-source peak response body streams must not exceed 2")
        }

        // Verify output integrity for all chapters
        for ((mangaId, chId) in mangaChapterMap) {
            verifyChapterOutput(mangaId, chId, chaptersPerSource)
        }

        assertTrue(DownloaderNetworkBudget.peakActiveUnits.get() <= 6, "Peak network permits must not exceed 6")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All network permits must be released")
    }

    @Test
    fun testNetworkBudgetPressureWithExcessWork() = runBlocking {
        val sixStreamsOpen = CountDownLatch(6)
        val releaseStreams = CountDownLatch(1)
        val closureWithoutPermit = AtomicBoolean(false)
        // Allow ten chapter claims to pressure the independent six-unit network
        // limiter. The separate global-concurrency test retains the real cap.
        DownloadManager.maxGlobalActiveOverride = 10
        val sources = (1..5).map { i ->
            FakeHttpSource(
                id = 9300L + i,
                pagesPerChapter = 2,
                pageStreamDelayMs = 0,
                onPageStreamStarted = { _, page ->
                    if (page == 0) {
                        sixStreamsOpen.countDown()
                        releaseStreams.await(10, TimeUnit.SECONDS)
                    }
                },
                onPageStreamClosed = { _, _ ->
                    if (DownloaderNetworkBudget.activeUnits.get() == 0) {
                        closureWithoutPermit.set(true)
                    }
                },
            ).also { registerFakeSource(it) }
        }
        val pairs = sources.flatMap { source ->
            val manga = createManga("ExcessManga-${source.id}", source.id)
            (1..2).map { manga to createChapter(manga, it, "Ch $it") }
        }
        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(pairs.map { it.second }))
            assertTrue(sixStreamsOpen.await(10, TimeUnit.SECONDS), "Six real bodies must hold permits")
            assertEquals(10, DownloadManager.activeChapterCount, "More than six claims must pressure the network gate")
            assertEquals(6, globalActiveStreams.get(), "Only six bodies may stream")
            assertEquals(6, DownloaderNetworkBudget.activeUnits.get())
            assertEquals(0, DownloaderNetworkBudget.availablePermits)
        } finally {
            releaseStreams.countDown()
        }
        withTimeout(30000) {
            while (pairs.any { !isChapterDownloaded(it.second) } || DownloadManager.getStatus().queue.isNotEmpty() || DownloadManager.activeClaims.isNotEmpty()) {
                delay(40)
            }
        }
        for ((manga, chapter) in pairs) verifyChapterOutput(manga, chapter, 2)
        assertFalse(closureWithoutPermit.get(), "A permit must cover response-body closure")
        assertEquals(6, DownloaderNetworkBudget.peakActiveUnits.get())
        assertEquals(6, DownloaderNetworkBudget.availablePermits)
        assertEquals(0, globalActiveStreams.get())
    }

    @Test
    fun testOneSourceSettingStillAllowsTwoChapters() = runBlocking {
        val bothOpen = CountDownLatch(2)
        val release = CountDownLatch(1)
        serverConfig.maxSourcesInParallel.value = 1
        val first = FakeHttpSource(
            id = 9351L, pagesPerChapter = 1, pageStreamDelayMs = 0,
            onPageStreamStarted = { _, _ -> bothOpen.countDown(); release.await(10, TimeUnit.SECONDS) },
        )
        val second = FakeHttpSource(id = 9352L, pagesPerChapter = 1, pageStreamDelayMs = 0)
        registerFakeSource(first)
        registerFakeSource(second)
        val firstManga = createManga("LimitedSourceFirst", first.id)
        val secondManga = createManga("LimitedSourceSecond", second.id)
        val chapters = listOf(createChapter(firstManga, 1), createChapter(firstManga, 2), createChapter(secondManga, 1))
        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(chapters))
            assertTrue(bothOpen.await(10, TimeUnit.SECONDS), "The source limit must not serialize chapters")
            assertEquals(2, first.activeStreams.get())
            assertEquals(0, second.peakStreams.get(), "A second source must wait under the one-source setting")
        } finally {
            release.countDown()
        }
        withTimeout(20000) {
            while (chapters.any { !isChapterDownloaded(it) } || DownloadManager.getStatus().queue.isNotEmpty() || DownloadManager.activeClaims.isNotEmpty()) delay(40)
        }
        assertEquals(1, second.peakStreams.get())
    }

    @Test
    fun testFairAdmissionFirstLaneBeforeSecondLane() = runBlocking {
        val s1 = FakeHttpSource(id = 9401L, pageStreamDelayMs = 100)
        val s2 = FakeHttpSource(id = 9402L, pageStreamDelayMs = 100)
        val s3 = FakeHttpSource(id = 9403L, pageStreamDelayMs = 100)
        registerFakeSource(s1)
        registerFakeSource(s2)
        registerFakeSource(s3)

        val m1 = createManga("M1", s1.id)
        val m2 = createManga("M2", s2.id)
        val m3 = createManga("M3", s3.id)

        val c1_1 = createChapter(m1, 1, "Ch 1.1")
        val c1_2 = createChapter(m1, 2, "Ch 1.2")
        val c2_1 = createChapter(m2, 1, "Ch 2.1")
        val c3_1 = createChapter(m3, 1, "Ch 3.1")

        // Restrict global active chapters to 2 for this test
        DownloadManager.maxGlobalActiveOverride = 2

        try {
            // Queue: S1(ch1), S1(ch2), S2(ch1), S3(ch1)
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(c1_1, c1_2, c2_1, c3_1)))

            // Wait for admission
            withTimeout(5000) {
                while (DownloadManager.activeClaims.size < 2) {
                    delay(20)
                }
            }

            // Priority 1 admits S1(ch1) and S2(ch1) (first lanes of ready sources) before S1(ch2)
            val admittedChapterIds = DownloadManager.activeClaims.keys.toSet()
            assertTrue(admittedChapterIds.contains(c1_1), "Source 1 first lane must be admitted")
            assertTrue(admittedChapterIds.contains(c2_1), "Source 2 first lane must be admitted")
            assertFalse(admittedChapterIds.contains(c1_2), "Source 1 second lane must NOT be admitted over a ready source's first lane")
        } finally {
            DownloadManager.maxGlobalActiveOverride = null
            DownloadManager.clear()
        }
    }

    @Test
    fun testQuickDequeueReenqueueDoesNotCancelNewJobOrOverlap() = runBlocking {
        val sourceId = 9501L
        val firstStreamStarted = CountDownLatch(1)
        val firstStreamCanProceed = CountDownLatch(1)
        val isFirstStream = AtomicBoolean(true)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 2,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0 && isFirstStream.compareAndSet(true, false)) {
                    firstStreamStarted.countDown()
                    firstStreamCanProceed.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        val mangaId = createManga("QuickDequeueManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        // 1. Enqueue old job
        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))

        // 2. Wait until old job begins streaming
        assertTrue(firstStreamStarted.await(5, TimeUnit.SECONDS), "Old response body must reach its cancellation gate")
        val oldClaim = DownloadManager.activeClaims[ch1]
        assertTrue(oldClaim != null, "Old claim must exist while streaming")

        // 3. Immediately dequeue old job and synchronously re-enqueue Chapter 1
        DownloadManager.dequeue(DownloadManager.EnqueueInput(listOf(ch1)))
        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))

        // Release the old stream to allow old job to finish settling/cancelling
        firstStreamCanProceed.countDown()

        // 4. Wait for the new chapter job to complete
        withTimeout(15000) {
            while (true) {
                if (isChapterDownloaded(ch1) && (DownloadManager.getStatus().queue.isEmpty() && DownloadManager.activeClaims.isEmpty())) {
                    break
                }
                delay(40)
            }
        }

        // Verify output integrity: new instance must finish without truncation or corrupt files
        verifyChapterOutput(mangaId, ch1, 2)
        assertTrue(DownloadManager.activeClaims.isEmpty(), "Active claims must be empty")
        assertEquals(0, DownloaderNetworkBudget.activeUnits.get(), "Active network units must be 0")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All network permits must be released")
    }

    @Test
    fun testDuplicateQueuedChapterIdsNeverRunTwoWorkers() = runBlocking {
        val opened = CountDownLatch(1)
        val release = CountDownLatch(1)
        val source = FakeHttpSource(
            id = 9551L, pagesPerChapter = 1, pageStreamDelayMs = 0,
            onPageStreamStarted = { _, _ -> opened.countDown(); release.await(10, TimeUnit.SECONDS) },
        )
        registerFakeSource(source)
        val manga = createManga("DuplicateQueue", source.id)
        val chapter = createChapter(manga, 1)
        // Model duplicate items left by a compound enqueue race. Admission must
        // enforce stable-ID ownership even when the incoming queue contains both.
        val field = DownloadManager::class.java.getDeclaredField("downloadQueue")
        field.isAccessible = true
        @Suppress("UNCHECKED_CAST")
        val queue = field.get(DownloadManager) as java.util.concurrent.CopyOnWriteArrayList<suwayomi.tachidesk.manga.impl.download.model.DownloadQueueItem>
        repeat(2) {
            queue.add(suwayomi.tachidesk.manga.impl.download.model.DownloadQueueItem(chapter, 1, manga, source.id, 0))
        }
        try {
            DownloadManager.start()
            assertTrue(opened.await(10, TimeUnit.SECONDS))
            assertEquals(1, DownloadManager.activeChapterCount)
            assertEquals(1, source.activeStreams.get())
        } finally {
            release.countDown()
        }
        withTimeout(15000) {
            while (queue.isNotEmpty() || DownloadManager.activeClaims.isNotEmpty()) delay(30)
        }
        assertEquals(1, source.peakStreams.get())
        verifyChapterOutput(manga, chapter, 1)
    }

    @Test
    fun testEnqueueAfterClearSnapshotSurvivesSettlement() = runBlocking {
        val oldOpened = CountDownLatch(1)
        val newOpened = CountDownLatch(1)
        val releaseOld = CountDownLatch(1)
        val releaseNew = CountDownLatch(1)
        val manga = createManga("ClearThenNewEnqueue", 9552L)
        val oldChapter = createChapter(manga, 1)
        val newChapter = createChapter(manga, 2)
        val source = FakeHttpSource(
            id = 9552L, pagesPerChapter = 1, pageStreamDelayMs = 0,
            onPageStreamStarted = { chapter, _ ->
                if (chapter == oldChapter) {
                    oldOpened.countDown(); releaseOld.await(10, TimeUnit.SECONDS)
                } else {
                    newOpened.countDown(); releaseNew.await(10, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(source)
        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(oldChapter)))
            assertTrue(oldOpened.await(10, TimeUnit.SECONDS))
            val oldClaim = DownloadManager.activeClaims[oldChapter]!!
            val clearing = async(Dispatchers.Default) { DownloadManager.clear() }
            withTimeout(5000) { while (!oldClaim.job.isCancelled) delay(10) }
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(newChapter)))
            assertTrue(newOpened.await(10, TimeUnit.SECONDS))
            releaseOld.countDown()
            clearing.await()
            assertTrue(DownloadManager.getStatus().queue.any { it.chapterId == newChapter },
                       "An enqueue after the clear snapshot must survive old-job settlement")
        } finally {
            releaseOld.countDown()
            releaseNew.countDown()
        }
        withTimeout(15000) {
            while (DownloadManager.getStatus().queue.isNotEmpty() || DownloadManager.activeClaims.isNotEmpty()) delay(30)
        }
        verifyChapterOutput(manga, newChapter, 1)
        assertEquals(0, DownloaderNetworkBudget.activeUnits.get())
    }

    @Test
    fun testConcurrentStopAndAdmissionLeavesZeroWorkersAndPermits() = runBlocking {
        val s1 = FakeHttpSource(id = 9601L, pageStreamDelayMs = 50)
        val s2 = FakeHttpSource(id = 9602L, pageStreamDelayMs = 50)
        registerFakeSource(s1)
        registerFakeSource(s2)

        val m1 = createManga("StopManga1", s1.id)
        val m2 = createManga("StopManga2", s2.id)

        val ch1 = createChapter(m1, 1, "Ch 1")
        val ch2 = createChapter(m1, 2, "Ch 2")
        val ch3 = createChapter(m2, 1, "Ch 3")
        val ch4 = createChapter(m2, 2, "Ch 4")

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2, ch3, ch4)))

        // Wait until active claims are admitted
        withTimeout(5000) {
            while (DownloadManager.activeClaims.size < 2) {
                delay(10)
            }
        }

        // Concurrently run stop() and start() + enqueue()
        val stopJob = async(Dispatchers.Default) {
            DownloadManager.stop()
        }
        val churnJob = async(Dispatchers.Default) {
            DownloadManager.start()
            val chExtra = createChapter(m1, 3, "Ch Extra")
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(chExtra)))
        }

        awaitAll(stopJob, churnJob)

        // Final authoritative stop
        DownloadManager.stop()

        // Assert zero workers and zero leaked permits
        assertEquals(Status.Stopped, DownloadManager.getStatus().status, "Status must be Stopped")
        assertTrue(DownloadManager.activeClaims.isEmpty(), "All active claims must be cleared on stop")
        assertEquals(0, DownloaderNetworkBudget.activeUnits.get(), "Active network permits must be 0")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All permits must be returned to semaphore")
    }

    @Test
    fun testErrorAndRetryMaxRetries() = runBlocking {
        val sourceId = 9701L
        val fakeSource = FakeHttpSource(id = sourceId, failPages = true)
        registerFakeSource(fakeSource)

        val mangaId = createManga("FailManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))

        // Wait for chapter to fail after 3 retries
        withTimeout(15000) {
            while (true) {
                val item = DownloadManager.getStatus().queue.firstOrNull { it.chapterId == ch1 }
                if (item != null && item.state == DownloadState.Error && item.tries >= 3) {
                    break
                }
                delay(50)
            }
        }

        val queueItem = DownloadManager.getStatus().queue.first { it.chapterId == ch1 }
        assertEquals(DownloadState.Error, queueItem.state, "Failed download must end in Error state")
        assertEquals(3, queueItem.tries, "Download must retry exactly 3 times before Error")
        assertEquals(0, DownloaderNetworkBudget.activeUnits.get(), "Active network units must be 0")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "Network permits must be released on error")
    }

    @Test
    fun testReorderRevokesAtSafeCheckpointAndResumes() = runBlocking {
        val sourceId = 9801L
        val fakeSource = FakeHttpSource(id = sourceId, pagesPerChapter = 2, pageStreamDelayMs = 60)
        registerFakeSource(fakeSource)

        val mangaId = createManga("ReorderManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")
        val ch2 = createChapter(mangaId, 2, "Ch 2")
        val ch3 = createChapter(mangaId, 3, "Ch 3")

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2, ch3)))

        // Wait until both ch1 and ch2 are actively downloading
        withTimeout(5000) {
            while (DownloadManager.activeClaims.size < 2) {
                delay(20)
            }
        }

        // Reorder ch3 to position 0 (ahead of ch2)
        DownloadManager.reorder(ch3, 0)

        // Wait for all 3 chapters to finish
        withTimeout(20000) {
            while (true) {
                val allDone = listOf(ch1, ch2, ch3).all { isChapterDownloaded(it) }
                if (allDone && (DownloadManager.getStatus().queue.isEmpty() && DownloadManager.activeClaims.isEmpty())) {
                    break
                }
                delay(50)
            }
        }

        // Verify output integrity of all 3 chapters
        verifyChapterOutput(mangaId, ch1, 2)
        verifyChapterOutput(mangaId, ch2, 2)
        verifyChapterOutput(mangaId, ch3, 2)
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All permits must be returned")
    }

    @Test
    fun testSerialMutantFailsDecisiveOverlapCheck() = runBlocking {
        // Test production limits: set maxChaptersPerSourceOverride = 1 (serial mutant)
        DownloadManager.maxChaptersPerSourceOverride = 1

        val sourceId = 9901L
        val fakeSource = FakeHttpSource(id = sourceId, pagesPerChapter = 2, pageStreamDelayMs = 40)
        registerFakeSource(fakeSource)

        val mangaId = createManga("SerialMutantManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")
        val ch2 = createChapter(mangaId, 2, "Ch 2")

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2)))

            withTimeout(15000) {
                while (true) {
                    if (isChapterDownloaded(ch1) && isChapterDownloaded(ch2) && (DownloadManager.getStatus().queue.isEmpty() && DownloadManager.activeClaims.isEmpty())) {
                        break
                    }
                    delay(40)
                }
            }

            // With maxChaptersPerSource = 1, peak streams NEVER reaches 2
            assertEquals(1, fakeSource.peakStreams.get(), "Serial mutant must never achieve concurrent stream overlap (peakStreams == 1)")
        } finally {
            DownloadManager.maxChaptersPerSourceOverride = null
        }
    }

    @Test
    fun testUnboundedMutantFailsDecisiveBoundsCheck() = runBlocking {
        // Test production limits: set maxChaptersPerSourceOverride = 5 (unbounded mutant)
        DownloadManager.maxChaptersPerSourceOverride = 5

        val sourceId = 9902L
        val streamsCount = AtomicInteger(0)
        val threeOverlapped = CountDownLatch(1)
        val releaseStreams = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 2,
            pageStreamDelayMs = 10,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0) {
                    if (streamsCount.incrementAndGet() >= 3) {
                        threeOverlapped.countDown()
                    }
                    releaseStreams.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        val mangaId = createManga("UnboundedMutantManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")
        val ch2 = createChapter(mangaId, 2, "Ch 2")
        val ch3 = createChapter(mangaId, 3, "Ch 3")
        val ch4 = createChapter(mangaId, 4, "Ch 4")

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2, ch3, ch4)))

            // With maxChaptersPerSource = 5, all chapters are admitted concurrently and at least 3 stream simultaneously
            val overlapped = threeOverlapped.await(10, TimeUnit.SECONDS)
            assertTrue(overlapped, "Unbounded mutant must allow >= 3 concurrent streams on single source")
            assertTrue(fakeSource.peakStreams.get() >= 3, "Peak same-source streams must reach at least 3")

            releaseStreams.countDown()
        } finally {
            releaseStreams.countDown()
            DownloadManager.maxChaptersPerSourceOverride = null
            DownloadManager.clear()
        }
    }

    @Test
    fun testConcurrentOutputCollisionSerializesAndPreservesOutputIntegrity() = runBlocking {
        val source1Id = 9801L
        val source2Id = 9802L

        val manga1Id = createManga("CollisionManga", source1Id)
        val ch1 = createChapter(manga1Id, 1, "Chapter 1")
        val ch2 = createChapter(manga1Id, 2, "Chapter 1")

        val manga2Id = createManga("DistinctManga", source2Id)
        val ch3 = createChapter(manga2Id, 1, "Chapter 1")


        val ch1StreamOpen = CountDownLatch(1)
        val releaseCh1 = CountDownLatch(1)
        val ch2Prepared = AtomicBoolean(false)
        val ch2StreamStarted = AtomicBoolean(false)
        val ch3Completed = CountDownLatch(1)

        val fakeSource1 = FakeHttpSource(
            id = source1Id,
            pagesPerChapter = 2,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { chapterId, pageNum ->
                if (chapterId == ch1 && pageNum == 0) {
                    ch1StreamOpen.countDown()
                    releaseCh1.await(5, TimeUnit.SECONDS)
                }
                if (chapterId == ch2) {
                    ch2StreamStarted.set(true)
                }
            },
        )
        fakeSource1.onGetPageListStarted = { chapterId ->
            if (chapterId == ch2) {
                ch2Prepared.set(true)
            }
        }
        registerFakeSource(fakeSource1)

        val fakeSource2 = FakeHttpSource(
            id = source2Id,
            pagesPerChapter = 2,
            pageStreamDelayMs = 20,
            onPageStreamClosed = { chapterId, pageNum ->
                if (chapterId == ch3 && pageNum == 1) {
                    ch3Completed.countDown()
                }
            },
        )
        registerFakeSource(fakeSource2)

        val path1 = Downloader.canonicalEffectiveOutputPath(manga1Id, ch1)
        val path2 = Downloader.canonicalEffectiveOutputPath(manga1Id, ch2)
        val path3 = Downloader.canonicalEffectiveOutputPath(manga2Id, ch3)

        assertEquals(path1, path2, "Both chapter IDs must resolve to the identical effective output path")
        assertTrue(path1 != path3, "Distinct chapter must have a different effective output path")

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
            assertTrue(ch1StreamOpen.await(5, TimeUnit.SECONDS), "Chapter 1 must acquire the shared path first")
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch2, ch3)))

            withTimeout(5000) {
                while (DownloadManager.activeClaims.size < 3) {
                    delay(20)
                }
            }
            assertTrue(DownloadManager.activeClaims.containsKey(ch1), "Chapter 1 claim must be active")
            assertTrue(DownloadManager.activeClaims.containsKey(ch2), "Chapter 2 claim must be active")
            assertTrue(DownloadManager.activeClaims.containsKey(ch3), "Chapter 3 claim must be active")

            assertTrue(ch1StreamOpen.await(5, TimeUnit.SECONDS), "Chapter 1 must begin streaming and hold path lock")

            // While Chapter 1 holds the lock:
            // 1) Chapter 2 cannot enter preparation or write
            assertFalse(ch2Prepared.get(), "Chapter 2 must not enter preparation while Chapter 1 holds the output path lock")
            assertFalse(ch2StreamStarted.get(), "Chapter 2 must not enter stream write while Chapter 1 holds the output path lock")
            withTimeout(5000) { while (Downloader.pathLockRefCount(path1) != 2) delay(10) }
            assertEquals(2, Downloader.pathLockRefCount(path1), "Path lock refCount must be 2 while Chapter 2 waits")

            // 2) Distinct-path job Chapter 3 must progress and complete
            assertTrue(ch3Completed.await(5, TimeUnit.SECONDS), "Distinct-path Chapter 3 must progress while path 1 is held")
            withTimeout(5000) {
                while (!isChapterDownloaded(ch3)) {
                    delay(20)
                }
            }
            assertTrue(isChapterDownloaded(ch3), "Chapter 3 must be marked downloaded")
            verifyChapterOutput(manga2Id, ch3, 2)

            assertFalse(ch2Prepared.get(), "Shared-path waiter must remain excluded while unrelated chapter completes")
            assertFalse(ch2StreamStarted.get(), "Shared-path waiter must not write before owner release")
            assertEquals(1, DownloaderNetworkBudget.activeUnits.get(), "Waiting for a path must not consume a network permit")

            // Releasing Chapter 1 permits Chapter 2 to proceed
            releaseCh1.countDown()

            withTimeout(10000) {
                while (true) {
                    if (isChapterDownloaded(ch1) && isChapterDownloaded(ch2)) {
                        break
                    }
                    delay(40)
                }
            }

            assertTrue(isChapterDownloaded(ch1), "Chapter 1 must finish successfully")
            assertTrue(isChapterDownloaded(ch2), "Chapter 2 must finish successfully")
            // Existing serial behavior recognizes the completed shared output without refetching chapter 2.
            assertFalse(ch2Prepared.get(), "Completed shared output must be reused after lock release")
            assertFalse(ch2StreamStarted.get(), "Alias must not overwrite completed shared output")
            verifyChapterOutput(manga1Id, ch1, 2)
            assertEquals(getChapterCbzPath(manga1Id, ch1), getChapterCbzPath(manga1Id, ch2))

            withTimeout(5000) {
                while (DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }

            assertTrue(DownloadManager.activeClaims.isEmpty(), "All active claims must be settled")
            assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All network permits must be released")
            assertEquals(0, Downloader.activePathLockCount(), "All path locks must be released and registry cleaned up")
        } finally {
            releaseCh1.countDown()
        }
    }

    @Test
    fun testCollisionCancellationWhileWaitingCleansUpRegistry() = runBlocking {
        val sourceId = 9803L
        val mangaId = createManga("CancelCollisionManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Chapter 1")
        val ch2 = createChapter(mangaId, 2, "Chapter 1")


        val ch1Streaming = CountDownLatch(1)
        val releaseCh1 = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 2,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { chapterId, pageNum ->
                if (chapterId == ch1 && pageNum == 0) {
                    ch1Streaming.countDown()
                    releaseCh1.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        val sharedPath = Downloader.canonicalEffectiveOutputPath(mangaId, ch1)
        assertEquals(sharedPath, Downloader.canonicalEffectiveOutputPath(mangaId, ch2))

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
            assertTrue(ch1Streaming.await(5, TimeUnit.SECONDS), "Chapter 1 must acquire the shared path first")
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch2)))

            withTimeout(5000) {
                while (DownloadManager.activeClaims.size < 2) {
                    delay(20)
                }
            }

            assertTrue(ch1Streaming.await(5, TimeUnit.SECONDS), "Chapter 1 must start streaming and hold path lock")
            withTimeout(5000) { while (Downloader.pathLockRefCount(sharedPath) != 2) delay(10) }
            assertEquals(2, Downloader.pathLockRefCount(sharedPath), "Path lock refCount must be 2 while ch2 waits")

            // Cancel Chapter 2 while waiting for path lock
            DownloadManager.dequeue(DownloadManager.EnqueueInput(listOf(ch2)))

            withTimeout(5000) {
                while (DownloadManager.activeClaims.containsKey(ch2)) {
                    delay(20)
                }
            }

            assertFalse(DownloadManager.activeClaims.containsKey(ch2), "Chapter 2 claim must be removed after cancellation")
            assertEquals(1, Downloader.pathLockRefCount(sharedPath), "Path lock refCount must drop to 1 after ch2 cancellation while waiting")

            releaseCh1.countDown()

            withTimeout(5000) {
                while (!isChapterDownloaded(ch1)) {
                    delay(20)
                }
            }

            assertTrue(isChapterDownloaded(ch1), "Chapter 1 must finish successfully")
            assertFalse(isChapterDownloaded(ch2), "Cancelled Chapter 2 must not be downloaded")
            verifyChapterOutput(mangaId, ch1, 2)

            withTimeout(5000) {
                while (DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }

            assertEquals(0, Downloader.pathLockRefCount(sharedPath), "Path lock refCount must be 0 after ch1 finishes")
            assertEquals(0, Downloader.activePathLockCount(), "Registry must have 0 active path locks after cancellation unwinding")
            assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "All network permits released")
        } finally {
            releaseCh1.countDown()
        }
    }

    @Test
    fun testPageWorkersSameChapterStreamOverlapAndMax2() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sourceId = 9801L
        val mangaId = createManga("PageOverlapManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        val p0Streaming = AtomicBoolean(false)
        val p1Streaming = AtomicBoolean(false)
        val overlapAchieved = CountDownLatch(1)
        val releaseStreams = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 4,
            pageStreamDelayMs = 25,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0) {
                    p0Streaming.set(true)
                    if (p1Streaming.get()) overlapAchieved.countDown()
                    releaseStreams.await(5, TimeUnit.SECONDS)
                } else if (pageNum == 1) {
                    p1Streaming.set(true)
                    if (p0Streaming.get()) overlapAchieved.countDown()
                    releaseStreams.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
            assertTrue(overlapAchieved.await(5, TimeUnit.SECONDS), "Page 0 and Page 1 must achieve concurrent stream overlap")
            assertEquals(2, fakeSource.peakStreams.get(), "Peak same-chapter page response streams must equal exactly 2")
            releaseStreams.countDown()

            withTimeout(10000) {
                while (!isChapterDownloaded(ch1) || DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }

            assertTrue(isChapterDownloaded(ch1))
            verifyChapterOutput(mangaId, ch1, 4)
            assertEquals(0, fakeSource.activeStreams.get(), "Active streams must settle to 0")
            assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "Permits must settle to 6")
        } finally {
            releaseStreams.countDown()
        }
    }

    @Test
    fun testPageWorkersGlobalPermitPressure() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sixHeldLatch = CountDownLatch(6)
        val releaseAllStreams = CountDownLatch(1)

        val onStart: (Int, Int) -> Unit = { _, _ ->
            sixHeldLatch.countDown()
            releaseAllStreams.await(5, TimeUnit.SECONDS)
        }

        val source1 = FakeHttpSource(9811L, pagesPerChapter = 4, pageStreamDelayMs = 20, onPageStreamStarted = onStart)
        val source2 = FakeHttpSource(9812L, pagesPerChapter = 4, pageStreamDelayMs = 20, onPageStreamStarted = onStart)
        registerFakeSource(source1)
        registerFakeSource(source2)

        val m1 = createManga("CapManga1", source1.id)
        val m2 = createManga("CapManga2", source2.id)
        val chs = listOf(
            createChapter(m1, 1, "M1 C1"),
            createChapter(m1, 2, "M1 C2"),
            createChapter(m2, 1, "M2 C1"),
            createChapter(m2, 2, "M2 C2"),
        )

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(chs))

            // 4 chapters * 2 workers = 8 potential concurrent operations.
            // Exactly 6 must acquire permits and hold streams; 2 must queue.
            assertTrue(sixHeldLatch.await(5, TimeUnit.SECONDS), "Must reach exactly 6 held streams under 8 potential worker operations")
            assertEquals(6, globalActiveStreams.get(), "Global active streams must reach exactly 6")
            assertEquals(0, DownloaderNetworkBudget.availablePermits, "Available network permits must reach exactly 0 under 6-stream saturation")
            assertEquals(6, DownloaderNetworkBudget.peakActiveUnits.get(), "Peak active network units must be 6")

            releaseAllStreams.countDown()

            withTimeout(20000) {
                while (chs.any { !isChapterDownloaded(it) } || DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }

            assertEquals(0, globalActiveStreams.get(), "Global active streams must settle to 0")
            assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "Network permits must settle to 6")
            for (ch in chs) {
                val mangaId = if (ch == chs[0] || ch == chs[1]) m1 else m2
                verifyChapterOutput(mangaId, ch, 4)
            }
        } finally {
            releaseAllStreams.countDown()
        }
    }

    @Test
    fun testPageWorkersMonotonicAndOutOfOrderProgress() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sourceId = 9821L
        val mangaId = createManga("MonotonicManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        val observedProgress = java.util.concurrent.CopyOnWriteArrayList<Float>()
        val p1Done = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 4,
            pageStreamDelayMs = 5,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0) {
                    p1Done.await(5, TimeUnit.SECONDS)
                }
            },
            onPageStreamClosed = { _, pageNum ->
                if (pageNum == 1) {
                    p1Done.countDown()
                }
            },
        )
        registerFakeSource(fakeSource)

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
            val item = DownloadManager.getStatus().queue.first { it.chapterId == ch1 }
            withTimeout(10000) {
                while (!isChapterDownloaded(ch1) || DownloadManager.activeClaims.isNotEmpty()) {
                    val p = item.progress
                    if (observedProgress.isEmpty() || observedProgress.last() != p) {
                        observedProgress.add(p)
                    }
                    delay(10)
                }
            }

            assertTrue(observedProgress.isNotEmpty(), "Must observe progress updates")
            var prev = -1f
            for (p in observedProgress) {
                assertTrue(p in 0f..1f, "Progress $p must be clamped between 0 and 1")
                assertTrue(p >= prev, "Progress must be strictly monotonic: prev=$prev, current=$p")
                prev = p
            }
            assertEquals(1f, item.progress, "Final chapter progress must be 1.0")
            verifyChapterOutput(mangaId, ch1, 4)
        } finally {
            p1Done.countDown()
        }
    }

    @Test
    fun testPageWorkersCachedPageSkipAndResume() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sourceId = 9831L
        val downloadedPages = java.util.concurrent.CopyOnWriteArrayList<Int>()
        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 4,
            pageStreamDelayMs = 15,
            onPageStreamStarted = { _, pageNum ->
                downloadedPages.add(pageNum)
            },
        )
        registerFakeSource(fakeSource)

        val mangaId = createManga("CachedManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        val cacheDir = File(getChapterCachePath(mangaId, ch1))
        cacheDir.mkdirs()

        val page0Bytes = createTestPngBytes(ch1, 0)
        val page2Bytes = createTestPngBytes(ch1, 2)
        File(cacheDir, "${Page.getPageName(0, 4)}.png").writeBytes(page0Bytes)
        File(cacheDir, "${Page.getPageName(2, 4)}.png").writeBytes(page2Bytes)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))

        withTimeout(10000) {
            while (!isChapterDownloaded(ch1) || DownloadManager.activeClaims.isNotEmpty()) {
                delay(20)
            }
        }

        assertTrue(isChapterDownloaded(ch1), "Chapter must complete download")
        assertFalse(downloadedPages.contains(0), "Cached page 0 must be skipped from download")
        assertFalse(downloadedPages.contains(2), "Cached page 2 must be skipped from download")
        assertTrue(downloadedPages.contains(1), "Page 1 must be downloaded")
        assertTrue(downloadedPages.contains(3), "Page 3 must be downloaded")
        verifyChapterOutput(mangaId, ch1, 4)
    }

    @Test
    fun testPageWorkersError429SiblingCancellationAndSettlement() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sourceId = 9841L
        val mangaId = createManga("ErrorManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        val p0Active = AtomicBoolean(false)
        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 4,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0) p0Active.set(true)
            },
            pageInterceptor = { _, pageNum ->
                if (pageNum == 1) {
                    Response.Builder()
                        .code(429)
                        .message("Too Many Requests")
                        .protocol(Protocol.HTTP_1_1)
                        .request(Request.Builder().url("https://fake-$sourceId.local/ch/$ch1/page/1.png").build())
                        .body(ResponseBody.create("text/plain".toMediaType(), "Rate limit exceeded"))
                        .build()
                } else null
            },
        )
        registerFakeSource(fakeSource)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))

        // Wait for final Error state, all 3 retries exhausted, and full permit/claim/stream settlement
        withTimeout(20000) {
            while (true) {
                val item = DownloadManager.getStatus().queue.firstOrNull { it.chapterId == ch1 }
                if (item != null && item.state == DownloadState.Error && item.tries >= 3 &&
                    DownloadManager.activeClaims.isEmpty() &&
                    fakeSource.activeStreams.get() == 0 &&
                    DownloaderNetworkBudget.availablePermits == DownloaderNetworkBudget.DEFAULT_MAX_PERMITS) {
                    break
                }
                delay(20)
            }
        }

        val queueItem = DownloadManager.getStatus().queue.firstOrNull { it.chapterId == ch1 }
        assertNotNull(queueItem, "Queue item must exist")
        assertEquals(DownloadState.Error, queueItem.state, "Final queue item state must be Error after exhausting 3 retries")
        assertEquals(3, queueItem.tries, "Tries must equal exactly 3")
        assertEquals(0, DownloadManager.activeClaims.size, "Active claims must settle to 0")
        assertEquals(0, fakeSource.activeStreams.get(), "Active streams must settle to 0")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "Permits must settle to 6")
        assertFalse(isChapterDownloaded(ch1), "Chapter must not be marked downloaded after error")
        val cbzFile = File(getChapterCbzPath(mangaId, ch1))
        assertFalse(cbzFile.exists(), "CBZ archive must not be created on error")
    }

    @Test
    fun testPageWorkersDequeueAndStop() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sourceId = 9851L
        val mangaId = createManga("DequeueManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        val p0Streaming = CountDownLatch(1)
        val holdStream = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 4,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0) {
                    p0Streaming.countDown()
                    holdStream.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
            assertTrue(p0Streaming.await(5, TimeUnit.SECONDS), "Page stream must start")

            DownloadManager.dequeue(DownloadManager.EnqueueInput(listOf(ch1)))
            holdStream.countDown()

            withTimeout(5000) {
                while (DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }

            assertEquals(0, DownloadManager.activeClaims.size, "Claims must settle on dequeue")
            assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "Permits must settle")
            assertFalse(isChapterDownloaded(ch1), "Dequeued chapter must not be finished")
        } finally {
            holdStream.countDown()
        }
    }

    @Test
    fun testPageWorkersHeldUnfinishedPagePreventsArchiveCreation() = runBlocking {
        ChaptersFilesProvider.pageWorkersOverride = null
        val sourceId = 9861L
        val mangaId = createManga("HeldPageManga", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")

        val p0Done = CountDownLatch(1)
        val holdP1 = CountDownLatch(1)

        val fakeSource = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 2,
            pageStreamDelayMs = 10,
            onPageStreamClosed = { _, pageNum ->
                if (pageNum == 0) p0Done.countDown()
            },
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 1) {
                    holdP1.await(5, TimeUnit.SECONDS)
                }
            },
        )
        registerFakeSource(fakeSource)

        val originalCbz = serverConfig.downloadAsCbz.value
        serverConfig.downloadAsCbz.value = false // Explicitly verify folder finalization path
        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
            assertTrue(p0Done.await(5, TimeUnit.SECONDS), "Page 0 must finish downloading")

            assertFalse(isChapterDownloaded(ch1), "Chapter must not be marked downloaded while page 1 is held")
            val chapterDir = File(getChapterDownloadPath(mangaId, ch1))
            val comicInfo = File(chapterDir, "ComicInfo.xml")
            assertFalse(comicInfo.exists(), "ComicInfo.xml must NOT exist while page 1 is unfinished")

            holdP1.countDown()

            withTimeout(10000) {
                while (!isChapterDownloaded(ch1) || DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }

            assertTrue(isChapterDownloaded(ch1), "Chapter must complete after releasing page 1")
            verifyChapterOutput(mangaId, ch1, 2)
            assertTrue(comicInfo.exists(), "Folder ComicInfo.xml must exist after completion")
        } finally {
            holdP1.countDown()
            serverConfig.downloadAsCbz.value = originalCbz
        }
    }

    @Test
    fun testPageWorkersSerial1AndUnbounded3Controls() = runBlocking {
        val sourceId = 9871L
        val mangaId = createManga("MutantManga", sourceId)

        // Control 1: Serial mutant (pageWorkersOverride = 1)
        ChaptersFilesProvider.pageWorkersOverride = 1
        val ch1 = createChapter(mangaId, 1, "Ch 1 Serial")
        val p0Streaming = AtomicBoolean(false)
        val p1Streaming = AtomicBoolean(false)
        val overlapAchieved = CountDownLatch(1)
        val fakeSourceSerial = FakeHttpSource(
            id = sourceId,
            pagesPerChapter = 2,
            pageStreamDelayMs = 25,
            onPageStreamStarted = { _, pageNum ->
                if (pageNum == 0) {
                    p0Streaming.set(true)
                    if (p1Streaming.get()) overlapAchieved.countDown()
                } else if (pageNum == 1) {
                    p1Streaming.set(true)
                    if (p0Streaming.get()) overlapAchieved.countDown()
                }
            },
            onPageStreamClosed = { _, pageNum ->
                if (pageNum == 0) p0Streaming.set(false)
                else if (pageNum == 1) p1Streaming.set(false)
            },
        )
        registerFakeSource(fakeSourceSerial)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))
        withTimeout(10000) {
            while (!isChapterDownloaded(ch1) || DownloadManager.activeClaims.isNotEmpty()) {
                delay(20)
            }
        }
        val serialOverlap = overlapAchieved.await(500, TimeUnit.MILLISECONDS)
        assertFalse(serialOverlap, "Serial-1 mutant must fail same-chapter stream overlap requirement")
        assertEquals(1, fakeSourceSerial.peakStreams.get(), "Serial mutant peak streams must equal 1")
        // Decisive invariant check: require failure of the same-chapter overlap invariant on serial control
        val overlapInvariantPasses = (serialOverlap && fakeSourceSerial.peakStreams.get() == 2)
        assertFalse(overlapInvariantPasses, "Same-chapter stream overlap invariant must decisively reject serial-1 mutant")

        GetSource.unregisterSource(sourceId)
        registeredSourceIds.remove(sourceId)

        // Control 2: Unbounded 3 mutant (pageWorkersOverride = 3)
        ChaptersFilesProvider.pageWorkersOverride = 3
        val sourceId3 = sourceId + 1
        val m2 = createManga("MutantManga3", sourceId3)
        val ch2 = createChapter(m2, 2, "Ch 2 Unbounded")
        val p0Wait = CountDownLatch(1)
        val p1Wait = CountDownLatch(1)
        val p2Wait = CountDownLatch(1)
        val releaseAll = CountDownLatch(1)

        val fakeSource3 = FakeHttpSource(
            id = sourceId3,
            pagesPerChapter = 4,
            pageStreamDelayMs = 20,
            onPageStreamStarted = { _, pageNum ->
                when (pageNum) {
                    0 -> { p0Wait.countDown(); releaseAll.await(5, TimeUnit.SECONDS) }
                    1 -> { p1Wait.countDown(); releaseAll.await(5, TimeUnit.SECONDS) }
                    2 -> { p2Wait.countDown(); releaseAll.await(5, TimeUnit.SECONDS) }
                }
            },
        )
        registerFakeSource(fakeSource3)

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch2)))
            assertTrue(p0Wait.await(5, TimeUnit.SECONDS))
            assertTrue(p1Wait.await(5, TimeUnit.SECONDS))
            assertTrue(p2Wait.await(5, TimeUnit.SECONDS))
            val peak = fakeSource3.peakStreams.get()
            assertEquals(3, peak, "3-worker mutant must reach peak 3 concurrent streams")
            // Decisive invariant check: require failure of max-2 concurrency invariant on unbounded-3 control
            val max2InvariantPasses = (peak <= 2)
            assertFalse(max2InvariantPasses, "Max-2 concurrency invariant must decisively reject unbounded-3 mutant")

            releaseAll.countDown()

            withTimeout(10000) {
                while (!isChapterDownloaded(ch2) || DownloadManager.activeClaims.isNotEmpty()) {
                    delay(20)
                }
            }
            assertTrue(isChapterDownloaded(ch2))
            verifyChapterOutput(m2, ch2, 4)
        } finally {
            releaseAll.countDown()
            ChaptersFilesProvider.resetForTest()
        }
    }
}
