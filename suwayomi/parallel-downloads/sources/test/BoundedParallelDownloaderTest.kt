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
import suwayomi.tachidesk.manga.impl.download.model.DownloadState
import suwayomi.tachidesk.manga.impl.download.model.Status
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

        override fun source(): BufferedSource {
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
            return stream.source().buffer()
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

            val bytes = createTestPngBytes(chapterId, pageNum)
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

        override suspend fun getPageList(chapter: SChapter): List<SPage> =
            (0 until pagesPerChapter).map { i ->
                SPage(i, "$baseUrl${chapter.url}/page/$i", "$baseUrl${chapter.url}/page/$i.png")
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
        globalActiveStreams.set(0)
        globalPeakStreams.set(0)
        DownloaderNetworkBudget.resetForTest()
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
        DownloaderNetworkBudget.resetForTest()
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
                if (isChapterDownloaded(ch1) && isChapterDownloaded(ch2) && DownloadManager.getStatus().queue.isEmpty()) {
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
                if (allDone && DownloadManager.getStatus().queue.isEmpty()) {
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
        // 5 sources * 2 chapters = 10 chapters competing for 6 global permits
        val numSources = 5
        val sources = (1..numSources).map { i ->
            val sourceId = 9300L + i
            val source = FakeHttpSource(id = sourceId, pagesPerChapter = 2, pageStreamDelayMs = 30)
            registerFakeSource(source)
            source
        }

        val allChapterIds = mutableListOf<Int>()
        val mangaChapterMap = mutableListOf<Pair<Int, Int>>()

        for (source in sources) {
            val mangaId = createManga("ExcessManga-${source.id}", source.id)
            for (c in 1..2) {
                val chId = createChapter(mangaId, c, "Ch $c")
                allChapterIds.add(chId)
                mangaChapterMap.add(mangaId to chId)
            }
        }

        DownloadManager.enqueue(DownloadManager.EnqueueInput(allChapterIds))

        withTimeout(30000) {
            while (true) {
                val allDone = allChapterIds.all { isChapterDownloaded(it) }
                if (allDone && DownloadManager.getStatus().queue.isEmpty()) {
                    break
                }
                delay(50)
            }
        }

        // Budget never exceeded despite 10 competing tasks
        assertTrue(DownloaderNetworkBudget.peakActiveUnits.get() <= 6, "Peak network permits must never exceed 6")
        assertTrue(globalPeakStreams.get() <= 6, "Peak global streaming response bodies must never exceed 6")

        for ((mangaId, chId) in mangaChapterMap) {
            verifyChapterOutput(mangaId, chId, 2)
        }

        assertEquals(0, DownloaderNetworkBudget.activeUnits.get(), "Active network units must be 0 on quiescence")
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits, "Permits must fully settle to 6")
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
        firstStreamStarted.await(5, TimeUnit.SECONDS)
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
                if (isChapterDownloaded(ch1) && DownloadManager.getStatus().queue.isEmpty()) {
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
                if (allDone && DownloadManager.getStatus().queue.isEmpty()) {
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
                    if (isChapterDownloaded(ch1) && isChapterDownloaded(ch2) && DownloadManager.getStatus().queue.isEmpty()) {
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
}
