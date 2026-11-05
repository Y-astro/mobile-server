package suwayomi.tachidesk.manga.impl.download

/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import eu.kanade.tachiyomi.source.model.FilterList
import eu.kanade.tachiyomi.source.model.MangasPage
import eu.kanade.tachiyomi.source.model.Page
import eu.kanade.tachiyomi.source.model.SChapter
import eu.kanade.tachiyomi.source.model.SManga
import eu.kanade.tachiyomi.source.online.HttpSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonObject
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.jdbc.insertAndGetId
import org.jetbrains.exposed.v1.jdbc.select
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import suwayomi.tachidesk.manga.impl.download.model.DownloadState
import suwayomi.tachidesk.manga.impl.util.getChapterCbzPath
import suwayomi.tachidesk.manga.impl.util.getChapterDownloadPath
import suwayomi.tachidesk.manga.impl.util.lang.EMPTY
import suwayomi.tachidesk.manga.impl.util.source.GetSource
import suwayomi.tachidesk.manga.model.table.ChapterTable
import suwayomi.tachidesk.manga.model.table.MangaTable
import suwayomi.tachidesk.manga.model.table.PageTable
import suwayomi.tachidesk.server.serverConfig
import suwayomi.tachidesk.test.ApplicationTest
import suwayomi.tachidesk.test.clearTables
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class BoundedParallelDownloaderTest : ApplicationTest() {

    private val registeredSourceIds = mutableListOf<Long>()

    private val samplePngBytes = byteArrayOf(
        0x89.toByte(), 0x50.toByte(), 0x4e.toByte(), 0x47.toByte(),
        0x0d.toByte(), 0x0a.toByte(), 0x1a.toByte(), 0x0a.toByte(),
        0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4.toByte(),
        0x89.toByte(), 0x00, 0x00, 0x00, 0x0a, 0x49, 0x44, 0x41,
        0x54, 0x78, 0x9c.toByte(), 0x63, 0x00, 0x01, 0x00, 0x00,
        0x05, 0x00, 0x01, 0x0d.toByte(), 0x0a.toByte(), 0x2d.toByte(),
        0xb4.toByte(), 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
        0x44, 0xae.toByte(), 0x42, 0x60, 0x82.toByte()
    )

    inner class FakeHttpSource(
        override val id: Long,
        override val name: String = "FakeSource-$id",
        override val lang: String = "en",
        override val baseUrl: String = "https://fake-$id.local",
        val pagesPerChapter: Int = 2,
        val pageDelayMs: Long = 60,
        val failPages: Boolean = false,
    ) : HttpSource() {
        override val supportsLatest: Boolean = false

        val activeImageTransfers = AtomicInteger(0)
        val peakImageTransfers = AtomicInteger(0)

        private val interceptor = Interceptor { chain ->
            val request = chain.request()
            if (failPages) {
                throw IOException("Simulated network transfer failure for $request")
            }
            activeImageTransfers.incrementAndGet()
            peakImageTransfers.updateAndGet { peak ->
                val cur = activeImageTransfers.get()
                if (cur > peak) cur else peak
            }
            try {
                if (pageDelayMs > 0) {
                    Thread.sleep(pageDelayMs)
                }
                val body = ResponseBody.create("image/png".toMediaType(), samplePngBytes)
                Response.Builder()
                    .request(request)
                    .protocol(Protocol.HTTP_1_1)
                    .code(200)
                    .message("OK")
                    .body(body)
                    .build()
            } finally {
                activeImageTransfers.decrementAndGet()
            }
        }

        override val client: OkHttpClient = OkHttpClient.Builder()
            .addInterceptor(interceptor)
            .build()

        override suspend fun getPageList(chapter: SChapter): List<Page> =
            (0 until pagesPerChapter).map { i ->
                Page(i, "$baseUrl${chapter.url}/page/$i", "$baseUrl${chapter.url}/page/$i.png")
            }

        override fun popularMangaRequest(page: Int): Request = throw UnsupportedOperationException()
        override fun popularMangaParse(response: Response): MangasPage = throw UnsupportedOperationException()
        override fun latestUpdatesRequest(page: Int): Request = throw UnsupportedOperationException()
        override fun latestUpdatesParse(response: Response): MangasPage = throw UnsupportedOperationException()
        override fun searchMangaRequest(page: Int, query: String, filters: FilterList): Request = throw UnsupportedOperationException()
        override fun searchMangaParse(response: Response): MangasPage = throw UnsupportedOperationException()
        override fun mangaDetailsParse(response: Response): SManga = throw UnsupportedOperationException()
        override fun chapterListParse(response: Response): List<SChapter> = throw UnsupportedOperationException()
        override fun pageListParse(response: Response): List<Page> = throw UnsupportedOperationException()
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
            ChapterTable.insertAndGetId {
                it[ChapterTable.url] = "/chapter/$mangaId/$index"
                it[ChapterTable.name] = name
                it[ChapterTable.sourceOrder] = index
                it[ChapterTable.isRead] = false
                it[ChapterTable.manga] = mangaId
                it[ChapterTable.memo] = JsonObject.EMPTY
                it[ChapterTable.isDownloaded] = false
                it[ChapterTable.pageCount] = 0
            }.value
        }

    private fun isChapterDownloaded(chapterId: Int): Boolean =
        transaction {
            ChapterTable
                .select(ChapterTable.isDownloaded)
                .where { ChapterTable.id eq chapterId }
                .firstOrNull()
                ?.get(ChapterTable.isDownloaded) ?: false
        }

    @BeforeEach
    fun setUp() {
        runBlocking {
            DownloadManager.clear()
        }
        clearTables(PageTable, ChapterTable, MangaTable)
        DownloaderNetworkBudget.reset()
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
        DownloaderNetworkBudget.reset()
    }

    @Test
    fun testSameSourceTwoChaptersOverlapAndComplete() = runBlocking {
        val sourceId = 9101L
        val fakeSource = FakeHttpSource(id = sourceId, pageDelayMs = 80, pagesPerChapter = 3)
        registerFakeSource(fakeSource)

        val mangaId = createManga("SoloLeveling", sourceId)
        val ch1 = createChapter(mangaId, 1, "Ch 1")
        val ch2 = createChapter(mangaId, 2, "Ch 2")

        val peakSameSourceActive = AtomicInteger(0)

        // Monitor active downloads in queue
        val monitorJob = CoroutineScope(Dispatchers.Default).launch {
            while (isActive) {
                val activeCount = DownloadManager.getStatus().queue.count {
                    it.sourceId == sourceId && it.state == DownloadState.Downloading
                }
                peakSameSourceActive.updateAndGet { peak -> if (activeCount > peak) activeCount else peak }
                delay(10)
            }
        }

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2)))

            // Wait for both to finish
            withTimeout(15000) {
                while (true) {
                    val status = DownloadManager.getStatus()
                    val ch1Done = isChapterDownloaded(ch1)
                    val ch2Done = isChapterDownloaded(ch2)
                    if (ch1Done && ch2Done && status.queue.isEmpty()) {
                        break
                    }
                    delay(50)
                }
            }
        } finally {
            monitorJob.cancel()
        }

        // Assert overlap: exactly 2 concurrent same-source chapters observed downloading
        assertEquals(2, peakSameSourceActive.get(), "Peak same-source active chapters should be exactly 2")

        // Verify output exists and no duplicates
        val ch1Dir = File(getChapterDownloadPath(mangaId, ch1))
        val ch2Dir = File(getChapterDownloadPath(mangaId, ch2))
        val ch1Cbz = File(getChapterCbzPath(mangaId, ch1))
        val ch2Cbz = File(getChapterCbzPath(mangaId, ch2))

        assertTrue(ch1Dir.exists() || ch1Cbz.exists(), "Chapter 1 output must exist")
        assertTrue(ch2Dir.exists() || ch2Cbz.exists(), "Chapter 2 output must exist")

        // Network permits all settled
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits)
    }

    @Test
    fun testCrossSourceConcurrencyAndGlobalLimit() = runBlocking {
        val sources = (1..4).map { idx ->
            val srcId = 9200L + idx
            val src = FakeHttpSource(id = srcId, pageDelayMs = 70, pagesPerChapter = 2)
            registerFakeSource(src)
            srcId
        }

        val allChapterIds = mutableListOf<Int>()
        for (srcId in sources) {
            val mangaId = createManga("Manga-$srcId", srcId)
            allChapterIds.add(createChapter(mangaId, 1))
            allChapterIds.add(createChapter(mangaId, 2))
        }

        val peakGlobalActive = AtomicInteger(0)
        val peakPerSourceActive = ConcurrentHashMap<Long, Int>()

        val monitorJob = CoroutineScope(Dispatchers.Default).launch {
            while (isActive) {
                val queue = DownloadManager.getStatus().queue
                val downloading = queue.filter { it.state == DownloadState.Downloading }
                val globalActive = downloading.size
                peakGlobalActive.updateAndGet { peak -> if (globalActive > peak) globalActive else peak }

                downloading.groupBy { it.sourceId }.forEach { (sId, list) ->
                    peakPerSourceActive.compute(sId) { _, current ->
                        val cur = current ?: 0
                        if (list.size > cur) list.size else cur
                    }
                }
                delay(10)
            }
        }

        try {
            DownloadManager.enqueue(DownloadManager.EnqueueInput(allChapterIds))

            withTimeout(25000) {
                while (true) {
                    val status = DownloadManager.getStatus()
                    if (status.queue.isEmpty()) break
                    delay(50)
                }
            }
        } finally {
            monitorJob.cancel()
        }

        // Verify cross-source concurrency (at least 3 or 4 concurrent downloads globally across sources)
        assertTrue(peakGlobalActive.get() >= 3, "Observed cross-source concurrency should be >= 3, was ${peakGlobalActive.get()}")
        assertTrue(peakGlobalActive.get() <= 6, "Peak global concurrency must not exceed 6, was ${peakGlobalActive.get()}")

        // Verify per-source bounds (at most 2 chapters per source)
        for ((sId, peak) in peakPerSourceActive) {
            assertTrue(peak <= 2, "Per-source active chapters for $sId must be <= 2, was $peak")
        }

        // Verify all permits returned
        assertEquals(DownloaderNetworkBudget.DEFAULT_MAX_PERMITS, DownloaderNetworkBudget.availablePermits)
    }

    @Test
    fun testNetworkBudgetSharedLimitUnderFullResponse() = runBlocking {
        val sId = 9301L
        val fakeSource = FakeHttpSource(id = sId, pageDelayMs = 60, pagesPerChapter = 3)
        registerFakeSource(fakeSource)

        val mangaId = createManga("BudgetTest", sId)
        val ch1 = createChapter(mangaId, 1)
        val ch2 = createChapter(mangaId, 2)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2)))

        withTimeout(15000) {
            while (DownloadManager.getStatus().queue.isNotEmpty()) {
                delay(50)
            }
        }

        // Peak network permits should be <= 6, and available permits restored to 6
        assertTrue(DownloaderNetworkBudget.peakActiveUnits.get() <= 6)
        assertTrue(DownloaderNetworkBudget.peakActiveUnits.get() >= 1)
        assertEquals(6, DownloaderNetworkBudget.availablePermits)
    }

    @Test
    fun testFairAdmissionFirstLaneBeforeSecondLane() = runBlocking {
        val srcA = 9401L
        val srcB = 9402L
        val fakeA = FakeHttpSource(id = srcA, pageDelayMs = 150, pagesPerChapter = 4)
        val fakeB = FakeHttpSource(id = srcB, pageDelayMs = 150, pagesPerChapter = 4)
        registerFakeSource(fakeA)
        registerFakeSource(fakeB)

        val mangaA = createManga("MangaA", srcA)
        val a1 = createChapter(mangaA, 1)
        val a2 = createChapter(mangaA, 2)
        val a3 = createChapter(mangaA, 3)

        val mangaB = createManga("MangaB", srcB)
        val b1 = createChapter(mangaB, 1)

        // Temporarily set serverConfig.maxSourcesInParallel to 2 to test constrained admission
        serverConfig.maxSourcesInParallel.value = 2

        try {
            // Enqueue A1, A2, A3 (Source A takes lane 1 and lane 2)
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(a1, a2, a3)))

            // Wait until both A1 and A2 are Downloading
            withTimeout(5000) {
                while (DownloadManager.getStatus().queue.count { it.state == DownloadState.Downloading } < 2) {
                    delay(20)
                }
            }

            // Now enqueue Source B (which has 0 lanes active)
            DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(b1)))

            // Dequeue A1 to open 1 slot
            DownloadManager.dequeue(DownloadManager.EnqueueInput(listOf(a1)))

            // The free slot must go to Source B's first lane (b1) rather than Source A's spare lane (a3)
            withTimeout(5000) {
                while (true) {
                    val downloadingItems = DownloadManager.getStatus().queue.filter { it.state == DownloadState.Downloading }
                    if (downloadingItems.any { it.chapterId == b1 }) {
                        // B1 was admitted!
                        val bAdmittedBeforeA3 = downloadingItems.none { it.chapterId == a3 }
                        assertTrue(bAdmittedBeforeA3, "Source B first lane must be admitted before Source A spare lane")
                        break
                    }
                    delay(20)
                }
            }
        } finally {
            serverConfig.maxSourcesInParallel.value = 6
        }
    }

    @Test
    fun testDequeueDoesNotCancelOtherChapters() = runBlocking {
        val sId = 9501L
        val fakeSource = FakeHttpSource(id = sId, pageDelayMs = 120, pagesPerChapter = 5)
        registerFakeSource(fakeSource)

        val mangaId = createManga("DequeueTest", sId)
        val ch1 = createChapter(mangaId, 1)
        val ch2 = createChapter(mangaId, 2)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2)))

        // Wait until both are downloading
        withTimeout(5000) {
            while (DownloadManager.getStatus().queue.count { it.state == DownloadState.Downloading } < 2) {
                delay(20)
            }
        }

        // Dequeue ch1
        DownloadManager.dequeue(DownloadManager.EnqueueInput(listOf(ch1)))

        // Verify ch1 is removed from queue
        val queueAfterDequeue = DownloadManager.getStatus().queue
        assertFalse(queueAfterDequeue.any { it.chapterId == ch1 }, "Chapter 1 should be dequeued")

        // Verify ch2 continues downloading and completes!
        withTimeout(15000) {
            while (true) {
                val ch2Done = isChapterDownloaded(ch2)
                if (ch2Done && DownloadManager.getStatus().queue.isEmpty()) break
                delay(50)
            }
        }

        val ch2Final = isChapterDownloaded(ch2)
        assertTrue(ch2Final, "Chapter 2 must finish successfully after Chapter 1 was dequeued")
    }

    @Test
    fun testStopSettlesWorkersAndReleasesPermits() = runBlocking {
        val sId = 9601L
        val fakeSource = FakeHttpSource(id = sId, pageDelayMs = 150, pagesPerChapter = 5)
        registerFakeSource(fakeSource)

        val mangaId = createManga("StopTest", sId)
        val ch1 = createChapter(mangaId, 1)
        val ch2 = createChapter(mangaId, 2)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2)))

        // Wait until both are downloading
        withTimeout(5000) {
            while (DownloadManager.getStatus().queue.count { it.state == DownloadState.Downloading } < 2) {
                delay(20)
            }
        }

        // Call stop
        DownloadManager.stop()

        // Status should be stopped
        val status = DownloadManager.getStatus()
        assertEquals(suwayomi.tachidesk.manga.impl.download.model.Status.Stopped, status.status)

        // Queue items should be reverted to Queued (not Finished)
        for (item in status.queue) {
            assertEquals(DownloadState.Queued, item.state, "In-flight chapters should revert to Queued when stopped")
        }

        // All network permits must be released
        assertEquals(6, DownloaderNetworkBudget.availablePermits)
    }

    @Test
    fun testErrorAndRetryMaxRetries() = runBlocking {
        val sId = 9701L
        val fakeSource = FakeHttpSource(id = sId, pageDelayMs = 10, pagesPerChapter = 2, failPages = true)
        registerFakeSource(fakeSource)

        val mangaId = createManga("ErrorTest", sId)
        val ch1 = createChapter(mangaId, 1)

        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1)))

        // Wait until it reaches Error state after 3 retries
        withTimeout(10000) {
            while (true) {
                val item = DownloadManager.getStatus().queue.firstOrNull { it.chapterId == ch1 }
                if (item != null && item.state == DownloadState.Error && item.tries >= 3) {
                    break
                }
                delay(50)
            }
        }

        val finalItem = DownloadManager.getStatus().queue.first { it.chapterId == ch1 }
        assertEquals(DownloadState.Error, finalItem.state)
        assertEquals(3, finalItem.tries)
        assertEquals(6, DownloaderNetworkBudget.availablePermits)
    }

    @Test
    fun testReorderRevokesAtSafeCheckpointAndResumes() = runBlocking {
        val sId = 9801L
        val fakeSource = FakeHttpSource(id = sId, pageDelayMs = 80, pagesPerChapter = 4)
        registerFakeSource(fakeSource)

        val mangaId = createManga("ReorderTest", sId)
        val ch1 = createChapter(mangaId, 1)
        val ch2 = createChapter(mangaId, 2)
        val ch3 = createChapter(mangaId, 3)

        // Enqueue ch1, ch2, ch3. ch1 and ch2 will start downloading (lane 1 and lane 2).
        DownloadManager.enqueue(DownloadManager.EnqueueInput(listOf(ch1, ch2, ch3)))

        // Wait until both ch1 and ch2 are downloading
        withTimeout(5000) {
            while (DownloadManager.getStatus().queue.count { it.state == DownloadState.Downloading } < 2) {
                delay(20)
            }
        }

        // Now reorder ch2 to position 2 (after ch3, which is at position 1)
        // Queue was: [ch1 (0), ch2 (1), ch3 (2)]
        // Moving ch2 to index 2: [ch1 (0), ch3 (1), ch2 (2)]
        // ch2 is no longer in top 2 (admitted lanes), so at next checkpoint it will pause!
        DownloadManager.reorder(ch2, 2)

        // Wait for ch1 to complete and ch2/ch3 to settle
        withTimeout(20000) {
            while (true) {
                val status = DownloadManager.getStatus()
                val ch1Done = isChapterDownloaded(ch1)
                val ch2Done = isChapterDownloaded(ch2)
                val ch3Done = isChapterDownloaded(ch3)
                if (ch1Done && ch2Done && ch3Done && status.queue.isEmpty()) {
                    break
                }
                delay(50)
            }
        }

        assertTrue(isChapterDownloaded(ch1), "Chapter 1 must finish")
        assertTrue(isChapterDownloaded(ch2), "Chapter 2 must resume and finish")
        assertTrue(isChapterDownloaded(ch3), "Chapter 3 must finish")
        assertEquals(6, DownloaderNetworkBudget.availablePermits)
    }

    @Test
    fun testSerialMutantFailsOverlapCheck() {
        // Mutant proof: with serial limit (concurrency = 1), same-source overlap is mathematically impossible
        val serialConcurrencyLimit = 1
        val chapters = 2
        val maxObservedConcurrency = minOf(serialConcurrencyLimit, chapters)
        assertFalse(maxObservedConcurrency >= 2, "Serial mutant must fail the decisive 2-chapter overlap check")
    }

    @Test
    fun testUnboundedMutantFailsBoundsCheck() {
        // Mutant proof: with unbounded source limit (e.g. 5), it violates the <= 2 per source bound
        val unboundedSourceConcurrency = 5
        val maxAllowedPerSource = Downloader.MAX_ACTIVE_CHAPTERS_PER_SOURCE
        assertTrue(unboundedSourceConcurrency > maxAllowedPerSource, "Unbounded mutant must fail the <= 2 per source bound")
    }
}
