package org.probe

/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import com.sun.management.OperatingSystemMXBean
import eu.kanade.tachiyomi.source.model.FilterList
import eu.kanade.tachiyomi.source.model.MangasPage
import eu.kanade.tachiyomi.source.model.Page as SPage
import eu.kanade.tachiyomi.source.model.SChapter
import eu.kanade.tachiyomi.source.model.SManga
import eu.kanade.tachiyomi.source.online.HttpSource
import kotlinx.coroutines.delay
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
import okio.BufferedSource
import okio.buffer
import okio.source
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.jdbc.insertAndGetId
import org.jetbrains.exposed.v1.jdbc.select
import org.jetbrains.exposed.v1.jdbc.transactions.TransactionManager
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import org.jetbrains.exposed.v1.jdbc.update
import suwayomi.tachidesk.manga.impl.Page
import suwayomi.tachidesk.manga.impl.download.DownloadManager
import suwayomi.tachidesk.manga.impl.download.Downloader
import suwayomi.tachidesk.manga.impl.download.DownloaderNetworkBudget
import suwayomi.tachidesk.manga.impl.download.fileProvider.ChaptersFilesProvider
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
import xyz.nulldev.androidcompat.config.FilesConfigModule
import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream
import java.lang.management.ManagementFactory
import java.net.ServerSocket
import java.nio.file.Path
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import java.util.zip.ZipFile
import javax.xml.parsers.DocumentBuilderFactory
import kotlin.system.exitProcess

data class SingleProbeResult(
    val scenario: String,
    val format: String,
    val pageWorkers: Int,
    val runType: String,
    val elapsedMs: Long,
    val cpuTimeMs: Long,
    val peakStreams: Int,
    val heapBeforeBytes: Long,
    val heapSettledBytes: Long,
    val vmRssBeforeKb: Long,
    val vmRssSettledKb: Long,
    val threadCountBefore: Int,
    val peakThreads: Int,
    val settledThreads: Int,
    val settledFds: Int,
    val settledClaims: Int,
    val settledPermits: Int,
    val settledStreams: Int,
    val outputIntegrityVerified: Boolean,
    val monotonicProgressVerified: Boolean,
)

object MainNativeProbe {

    val globalActiveStreams = AtomicInteger(0)
    val globalPeakStreams = AtomicInteger(0)

    private fun sha256Hex(bytes: ByteArray): String {
        val md = MessageDigest.getInstance("SHA-256")
        return md.digest(bytes).joinToString("") { "%02x".format(it) }
    }

    private fun readVmRssKb(): Long {
        return try {
            File("/proc/self/status").readLines()
                .firstOrNull { it.startsWith("VmRSS:") }
                ?.substringAfter(":")?.trim()?.substringBefore(" ")?.toLongOrNull() ?: -1L
        } catch (_: Exception) {
            -1L
        }
    }

    private fun readFdCount(): Int {
        return try {
            File("/proc/self/fd").listFiles()?.size ?: -1
        } catch (_: Exception) {
            -1
        }
    }

    private fun verifyComicInfo(xmlBytes: ByteArray, expectedSeries: String, expectedTitle: String, expectedNumber: String) {
        val factory = DocumentBuilderFactory.newInstance()
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)
        val doc = factory.newDocumentBuilder().parse(ByteArrayInputStream(xmlBytes))
        doc.documentElement.normalize()
        check(doc.documentElement.nodeName == "ComicInfo") { "Root must be ComicInfo" }
        fun getTag(tag: String): String? {
            val n = doc.getElementsByTagName(tag)
            return if (n.length > 0) n.item(0).textContent else null
        }
        check(getTag("Series") == expectedSeries) { "Series mismatch: expected $expectedSeries, got ${getTag("Series")}" }
        check(getTag("Title") == expectedTitle) { "Title mismatch: expected $expectedTitle, got ${getTag("Title")}" }
        check(getTag("Number") == expectedNumber) { "Number mismatch: expected $expectedNumber, got ${getTag("Number")}" }
    }

    private suspend fun verifyOutputIntegrity(
        mangaId: Int,
        chapterId: Int,
        logicalChapter: Int,
        mangaTitle: String,
        chapterName: String,
        expectedPages: Int,
        isCbz: Boolean,
        payloadShas: List<String>,
    ): Boolean {
        val expectedNumber = logicalChapter.toString()
        if (isCbz) {
            val cbzFile = File(getChapterCbzPath(mangaId, chapterId))
            check(cbzFile.exists() && cbzFile.isFile) { "CBZ file must exist: $cbzFile" }
            ZipFile(cbzFile).use { zip ->
                val entries = zip.entries().asSequence().map { it.name }.toSet()
                check(entries.contains("ComicInfo.xml")) { "CBZ missing ComicInfo.xml" }
                val comicBytes = zip.getInputStream(zip.getEntry("ComicInfo.xml")!!).readBytes()
                verifyComicInfo(comicBytes, mangaTitle, chapterName, expectedNumber)

                for (p in 0 until expectedPages) {
                    val pageName = "${Page.getPageName(p, expectedPages)}.png"
                    check(entries.contains(pageName)) { "CBZ missing $pageName" }
                    val actualBytes = zip.getInputStream(zip.getEntry(pageName)!!).readBytes()
                    val expectedSha = payloadShas[(logicalChapter * expectedPages + p) % payloadShas.size]
                    val actualSha = sha256Hex(actualBytes)
                    check(actualSha == expectedSha) { "Page $p SHA mismatch: expected $expectedSha, got $actualSha" }
                }
                val pngCount = entries.filter { it.endsWith(".png") }.size
                check(pngCount == expectedPages) { "Expected $expectedPages pngs, got $pngCount" }
            }
        } else {
            val dir = File(getChapterDownloadPath(mangaId, chapterId))
            check(dir.exists() && dir.isDirectory) { "Folder must exist: $dir" }
            val files = dir.listFiles()?.toList().orEmpty()
            val fileNames = files.map { it.name }.toSet()
            check(fileNames.contains("ComicInfo.xml")) { "Folder missing ComicInfo.xml" }
            val comicBytes = File(dir, "ComicInfo.xml").readBytes()
            verifyComicInfo(comicBytes, mangaTitle, chapterName, expectedNumber)

            for (p in 0 until expectedPages) {
                val pageName = "${Page.getPageName(p, expectedPages)}.png"
                check(fileNames.contains(pageName)) { "Folder missing $pageName" }
                val actualBytes = File(dir, pageName).readBytes()
                val expectedSha = payloadShas[(logicalChapter * expectedPages + p) % payloadShas.size]
                val actualSha = sha256Hex(actualBytes)
                check(actualSha == expectedSha) { "Page $p SHA mismatch: expected $expectedSha, got $actualSha" }
            }
            val pngCount = fileNames.filter { it.endsWith(".png") }.size
            check(pngCount == expectedPages) { "Expected $expectedPages pngs, got $pngCount" }
        }
        return true
    }

    class ProbeFakeHttpSource(
        override val id: Long,
        override val name: String,
        var pagesPerChapter: Int = 4,
        var payloadLookup: (chId: Int, pageNum: Int) -> ByteArray = { _, _ -> ByteArray(0) },
        var streamDelayMs: Long = 20L,
    ) : HttpSource() {
        override val lang: String = "en"
        override val baseUrl: String = "https://probe-$id.local"
        override val supportsLatest: Boolean = false

        val activeStreams = AtomicInteger(0)

        private val interceptor = Interceptor { chain ->
            val request = chain.request()
            val pathSegments = request.url.pathSegments
            val chId = pathSegments.getOrNull(2)?.toIntOrNull() ?: 1
            val pNum = pathSegments.getOrNull(4)?.substringBefore(".")?.toIntOrNull() ?: 0

            val bytes = payloadLookup(chId, pNum)
            val body = object : ResponseBody() {
                override fun contentType() = "image/png".toMediaType()
                override fun contentLength() = bytes.size.toLong()

                private val bais = ByteArrayInputStream(bytes)
                private val stream = object : InputStream() {
                    private var opened = false
                    private fun ensureOpen() {
                        if (!opened) {
                            opened = true
                            activeStreams.incrementAndGet()
                            val g = globalActiveStreams.incrementAndGet()
                            globalPeakStreams.updateAndGet { p -> if (g > p) g else p }
                            if (streamDelayMs > 0) Thread.sleep(streamDelayMs)
                        }
                    }
                    override fun read(): Int { ensureOpen(); return bais.read() }
                    override fun read(b: ByteArray, off: Int, len: Int): Int { ensureOpen(); return bais.read(b, off, len) }
                    override fun close() {
                        try { bais.close() } finally {
                            if (opened) {
                                opened = false
                                activeStreams.decrementAndGet()
                                globalActiveStreams.decrementAndGet()
                            }
                        }
                    }
                }
                private val bufferedSource = stream.source().buffer()
                override fun source(): BufferedSource = bufferedSource
            }
            Response.Builder()
                .request(request)
                .protocol(Protocol.HTTP_1_1)
                .code(200)
                .message("OK")
                .body(body)
                .build()
        }

        override val client: OkHttpClient = OkHttpClient.Builder().addInterceptor(interceptor).build()

        override suspend fun getPageList(chapter: SChapter): List<SPage> =
            (0 until pagesPerChapter).map { i -> SPage(i, "$baseUrl${chapter.url}/page/$i", "$baseUrl${chapter.url}/page/$i.png") }

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
                    it[ChapterTable.chapter_number] = index.toFloat()
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

    private fun safeCleanDir(target: File, allowedParent: File, label: String) {
        val canonicalTarget = target.canonicalFile
        val canonicalParent = allowedParent.canonicalFile
        check(canonicalTarget.toPath().startsWith(canonicalParent.toPath())) {
            "Refusing to clean $label: $canonicalTarget is outside $canonicalParent"
        }
        check(canonicalTarget != canonicalParent) {
            "Refusing to delete parent directory for $label: $canonicalTarget"
        }
        if (canonicalTarget.exists()) {
            canonicalTarget.deleteRecursively()
        }
        canonicalTarget.mkdirs()
    }

    @JvmStatic
    fun main(args: Array<String>) {
        val startWallNs = System.nanoTime()
        println("=== Starting MainNativeProbe ===")

        var allPassedOverall = false
        var bootstrapped = false
        try {
            val customWorkspace = System.getProperty("mobile.proof.workspace")
                ?: System.getenv("MOBILE_PROOF_WORKSPACE")
                ?: throw IllegalStateException("mobile.proof.workspace must be explicitly set before execution")
            val workspaceDir = File(customWorkspace).canonicalFile
            val workspacePath = workspaceDir.toPath().toRealPath()

            val initialCwd = File(".").canonicalFile.toPath()
            check(initialCwd == workspacePath) {
                "Process CWD $initialCwd must be inside private workspace $workspacePath before JVM startup"
            }

            val workspaceHome = File(workspaceDir, "home").canonicalFile
            val workspaceTmp = File(workspaceDir, "tmp").canonicalFile
            val workspaceData = File(workspaceDir, "data").canonicalFile
            listOf(workspaceHome, workspaceTmp, workspaceData).forEach { it.mkdirs() }

            check(File(System.getenv("HOME")).canonicalFile == workspaceHome)
            check(File(System.getProperty("user.home")).canonicalFile == workspaceHome)
            check(File(System.getProperty("java.io.tmpdir")).canonicalFile == workspaceTmp)
            val flags = ManagementFactory.getRuntimeMXBean().inputArguments
            check(flags.containsAll(listOf("-Xms64m", "-Xmx384m", "-XX:+UseSerialGC",
                "-XX:TieredStopAtLevel=1", "-XX:CICompilerCount=2")))
            check(Runtime.getRuntime().maxMemory() in (383L * 1024 * 1024)..(384L * 1024 * 1024))
            check(!File("/proc/self/cmdline").readText().contains("suwayomi", ignoreCase = true))
            val candidateFile = File(System.getProperty("mobile.proof.candidate.jar")).canonicalFile
            val digest = MessageDigest.getInstance("SHA-256")
            candidateFile.inputStream().use { input ->
                val buffer = ByteArray(65536)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    digest.update(buffer, 0, count)
                }
            }
            val artifactSha = digest.digest().joinToString("") { "%02x".format(it) }
            check(artifactSha == System.getProperty("mobile.proof.expected.sha"))
            val origins = listOf(
                "suwayomi.tachidesk.manga.impl.download.DownloadManager",
                "suwayomi.tachidesk.manga.impl.download.Downloader",
                "suwayomi.tachidesk.manga.impl.download.DownloaderNetworkBudget",
                "suwayomi.tachidesk.manga.impl.download.ChapterClaim",
                "suwayomi.tachidesk.manga.impl.download.fileProvider.ChaptersFilesProvider",
                "kotlin.Unit", "kotlinx.coroutines.Job", "okhttp3.OkHttpClient",
                "org.objectweb.asm.ClassReader", "com.sun.jna.Native",
                "org.jetbrains.exposed.v1.jdbc.Database",
            )
            origins.forEach { name ->
                val type = Class.forName(name, false, MainNativeProbe::class.java.classLoader)
                check(File(type.protectionDomain.codeSource.location.toURI()).canonicalFile == candidateFile) { name }
            }
            println("Verified packaged class origins: ${origins.size}; SHA-256: $artifactSha")

            val unboundPort = ServerSocket(0).use { it.localPort }
            System.setProperty("user.home", workspaceHome.canonicalPath)
            System.setProperty("java.io.tmpdir", workspaceTmp.canonicalPath)
            System.setProperty("java.util.prefs.userRoot", File(workspaceHome, "prefs").canonicalPath)
            System.setProperty("java.util.prefs.systemRoot", File(workspaceHome, "system-prefs").canonicalPath)
            System.setProperty("suwayomi.tachidesk.config.server.port", unboundPort.toString())
            System.setProperty("suwayomi.tachidesk.config.server.rootDir", workspaceData.canonicalPath)

            println("Private workspace: $workspacePath")
            println("Private unbound port: $unboundPort")

            ApplicationTest.testingSetup()
            ApplicationTest.databaseSetup()
            bootstrapped = true

            val appDirs = Injekt.get<ApplicationDirs>()
            listOf(
                File(appDirs.dataRoot),
                File(appDirs.tempRoot),
                File(appDirs.downloadsRoot),
                File(appDirs.cacheDir),
                File(System.getProperty("user.home")),
                File(System.getProperty("java.util.prefs.userRoot")),
            ).forEach { it.mkdirs() }
            val filesConfig = xyz.nulldev.ts.config.GlobalConfigManager.loadedModules[FilesConfigModule::class.java] as? FilesConfigModule
                ?: FilesConfigModule.register(xyz.nulldev.ts.config.GlobalConfigManager.config).also {
                    xyz.nulldev.ts.config.GlobalConfigManager.registerModules(it)
                }
            listOf(File(filesConfig.rootDir), File(filesConfig.externalStorageDir)).forEach { it.mkdirs() }

            val dataRootPath = File(appDirs.dataRoot).canonicalFile.toPath()
            val tempRootPath = File(appDirs.tempRoot).canonicalFile.toPath()
            val downloadsPath = File(appDirs.downloadsRoot).canonicalFile.toPath()
            val cachePath = File(appDirs.cacheDir).canonicalFile.toPath()
            val userHomePath = File(System.getProperty("user.home")).canonicalFile.toPath()
            val prefsPath = File(System.getProperty("java.util.prefs.userRoot")).canonicalFile.toPath()
            val androidFilesRoot = File(filesConfig.rootDir).canonicalFile.toPath()
            val androidExtRoot = File(filesConfig.externalStorageDir).canonicalFile.toPath()

            val validatedPaths = mapOf(
                "dataRoot" to dataRootPath,
                "tempRoot" to tempRootPath,
                "downloadsRoot" to downloadsPath,
                "cacheDir" to cachePath,
                "user.home" to userHomePath,
                "prefs" to prefsPath,
                "androidFilesRoot" to androidFilesRoot,
                "androidExtRoot" to androidExtRoot,
            )

            for ((label, p) in validatedPaths) {
                check(p.startsWith(workspacePath)) {
                    "Isolation boundary check failed: $label ($p) is outside workspace ($workspacePath)"
                }
            }
            println("All filesystem boundaries verified inside private workspace.")

            val actualDbUrl = transaction {
                TransactionManager.current().db.url
            }
            check(actualDbUrl.startsWith("jdbc:h2:mem:")) {
                "Isolation check failed: database URL $actualDbUrl must be in-memory H2"
            }
            println("In-memory H2 database verified: $actualDbUrl")

            val payloadDir = File(System.getProperty("mobile.proof.payloads")
                ?: File(workspaceDir, "payloads").canonicalPath)
                .let { if (it.exists()) it else File(".work/payloads").canonicalFile }
            check(payloadDir.exists()) { "Payload directory does not exist: $payloadDir" }
            val payloadFiles = payloadDir.listFiles()?.filter { it.name.endsWith(".png") }?.sortedBy { it.name }.orEmpty()
            check(payloadFiles.isNotEmpty()) { "No PNG payloads found in $payloadDir" }
            val payloads = payloadFiles.map { it.readBytes() }
            val payloadShas = payloads.map { sha256Hex(it) }
            println("Loaded ${payloads.size} deterministic PNG payloads from $payloadDir (avg size: ${payloads[0].size} bytes)")

            // Bounded fixed sources reused across trials
            val fixedSources = (0 until 3).map { sIdx ->
                val sId = 8000L + sIdx
                val src = ProbeFakeHttpSource(id = sId, name = "ProbeSource-$sIdx")
                GetSource.registerSource(sId to src)
                src
            }

            val chapterIdToLogical = ConcurrentHashMap<Int, Int>()

            fun runSingleTrial(
                scenario: String,
                format: String,
                pageWorkers: Int,
                runType: String,
                numSources: Int,
                chaptersPerSource: Int,
                pagesPerChapter: Int,
            ): SingleProbeResult {
                serverConfig.downloadAsCbz.value = (format == "CBZ")
                serverConfig.maxSourcesInParallel.value = 6
                DownloadManager.maxChaptersPerSourceOverride = 2
                DownloadManager.maxGlobalActiveOverride = 6
                ChaptersFilesProvider.pageWorkersOverride = pageWorkers

                runBlocking { DownloadManager.clear() }
                clearTables(PageTable, ChapterTable, MangaTable)
                safeCleanDir(File(appDirs.downloadsRoot), workspaceDir, "downloads")
                safeCleanDir(File(appDirs.tempMangaCacheRoot), workspaceDir, "cache")
                DownloaderNetworkBudget.resetForTest()
                Downloader.resetPathLocksForTest()

                check(globalActiveStreams.get() == 0) { "Quiescence failed: active streams != 0" }
                check(DownloadManager.activeClaims.isEmpty()) { "Quiescence failed: active claims not empty" }
                check(DownloaderNetworkBudget.availablePermits == 6) { "Quiescence failed: network permits != 6" }
                check(Downloader.activePathLockCount() == 0) { "Quiescence failed: path locks remain" }

                globalPeakStreams.set(0)
                chapterIdToLogical.clear()

                val activeSources = fixedSources.take(numSources)
                activeSources.forEach { src ->
                    src.pagesPerChapter = pagesPerChapter
                    src.streamDelayMs = 20L
                    src.payloadLookup = { chId, pageNum ->
                        val logicalCh = chapterIdToLogical[chId] ?: 0
                        val payloadIdx = ((logicalCh * pagesPerChapter) + pageNum) % payloads.size
                        payloads[payloadIdx]
                    }
                }

                val chapterSpecs = mutableListOf<Triple<Int, Int, String>>() // (mangaId, chapterId, chapterName)
                for (sIdx in 0 until numSources) {
                    val sId = activeSources[sIdx].id
                    val mId = createManga("ProbeManga-$sIdx", sId)
                    for (cIdx in 0 until chaptersPerSource) {
                        val cName = "Ch $cIdx"
                        val chId = createChapter(mId, cIdx, cName)
                        chapterIdToLogical[chId] = cIdx
                        chapterSpecs.add(Triple(mId, chId, cName))
                    }
                }
                val chapterIds = chapterSpecs.map { it.second }

                System.gc()
                val heapBefore = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()
                val rssBefore = readVmRssKb()
                val threadBean = ManagementFactory.getThreadMXBean()
                val threadsBefore = threadBean.threadCount
                val osBean = ManagementFactory.getOperatingSystemMXBean() as? OperatingSystemMXBean
                val cpuBeforeNs = osBean?.processCpuTime ?: 0L

                val trialStartWallNs = System.nanoTime()
                val chapterProgressSamples = ConcurrentHashMap<Int, MutableList<Float>>()

                runBlocking {
                    DownloadManager.enqueue(DownloadManager.EnqueueInput(chapterIds))
                    withTimeout(30_000L) {
                        while (
                            chapterIds.any { !isChapterDownloaded(it) } ||
                            DownloadManager.getStatus().queue.isNotEmpty() ||
                            DownloadManager.activeClaims.isNotEmpty()
                        ) {
                            for (item in DownloadManager.getStatus().queue) {
                                val list = chapterProgressSamples.computeIfAbsent(item.chapterId) { mutableListOf() }
                                val p = item.progress
                                if (list.isEmpty() || list.last() != p) {
                                    list.add(p)
                                }
                            }
                            delay(15)
                        }
                    }
                }

                val trialElapsedMs = (System.nanoTime() - trialStartWallNs) / 1_000_000L
                val cpuAfterNs = osBean?.processCpuTime ?: 0L
                val trialCpuMs = if (cpuAfterNs > cpuBeforeNs) (cpuAfterNs - cpuBeforeNs) / 1_000_000L else 0L

                val settledClaims = DownloadManager.activeChapterCount
                val settledPermits = DownloaderNetworkBudget.availablePermits
                val settledStreams = globalActiveStreams.get()
                val peakStreams = globalPeakStreams.get()
                val peakThreads = threadBean.peakThreadCount

                check(settledClaims == 0) { "Settled claims must be 0, got $settledClaims" }
                check(settledPermits == 6) { "Settled network permits must be 6, got $settledPermits" }
                check(settledStreams == 0) { "Settled active streams must be 0, got $settledStreams" }

                var monotonicOk = true
                for (cId in chapterIds) {
                    val samples = chapterProgressSamples[cId].orEmpty()
                    if (samples.isEmpty()) {
                        monotonicOk = false
                        break
                    }
                    var prevP = -1f
                    for (p in samples) {
                        if (p < 0f || p > 1f || p < prevP) {
                            monotonicOk = false
                            break
                        }
                        prevP = p
                    }
                    if (!monotonicOk) break
                }

                var integrityOk = true
                runBlocking {
                    chapterSpecs.forEachIndexed { idx, (mId, cId, cName) ->
                        val cIdx = idx % chaptersPerSource
                        val ok = verifyOutputIntegrity(
                            mangaId = mId,
                            chapterId = cId,
                            logicalChapter = cIdx,
                            mangaTitle = "ProbeManga-${idx / chaptersPerSource}",
                            chapterName = cName,
                            expectedPages = pagesPerChapter,
                            isCbz = (format == "CBZ"),
                            payloadShas = payloadShas,
                        )
                        if (!ok) integrityOk = false
                    }
                }

                check(integrityOk) { "Output integrity check failed for trial $scenario $format $runType" }
                check(monotonicOk) { "Monotonic progress check failed for trial $scenario $format $runType" }

                runBlocking { DownloadManager.clear() }
                clearTables(PageTable, ChapterTable, MangaTable)
                safeCleanDir(File(appDirs.downloadsRoot), workspaceDir, "downloads")
                safeCleanDir(File(appDirs.tempMangaCacheRoot), workspaceDir, "cache")
                DownloaderNetworkBudget.resetForTest()
                Downloader.resetPathLocksForTest()

                System.gc()
                Thread.sleep(40)
                System.gc()

                val heapSettled = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()
                val rssSettled = readVmRssKb()
                val threadsSettled = threadBean.threadCount
                val fdsSettled = readFdCount()

                return SingleProbeResult(
                    scenario = scenario,
                    format = format,
                    pageWorkers = pageWorkers,
                    runType = runType,
                    elapsedMs = trialElapsedMs,
                    cpuTimeMs = trialCpuMs,
                    peakStreams = peakStreams,
                    heapBeforeBytes = heapBefore,
                    heapSettledBytes = heapSettled,
                    vmRssBeforeKb = rssBefore,
                    vmRssSettledKb = rssSettled,
                    threadCountBefore = threadsBefore,
                    peakThreads = peakThreads,
                    settledThreads = threadsSettled,
                    settledFds = fdsSettled,
                    settledClaims = settledClaims,
                    settledPermits = settledPermits,
                    settledStreams = settledStreams,
                    outputIntegrityVerified = integrityOk,
                    monotonicProgressVerified = monotonicOk,
                )
            }

            val allResults = mutableListOf<SingleProbeResult>()

            val testScenarios = listOf(
                Triple("OneSourceOneChapter", 1, 1 to 4),       // 1 source, 1 chapter, 4 pages
                Triple("OneSourceTwoChapters", 1, 2 to 4),      // 1 source, 2 chapters, 4 pages
                Triple("MultisourceCapPressure", 3, 2 to 2),    // 3 sources, 2 chapters, 2 pages (6 chapters total)
            )
            val formats = listOf("CBZ", "Folder")

            println("\n--- Commencing Paired Trial Matrix (Warmup + ABBA) ---")
            for ((scName, numSrc, chPg) in testScenarios) {
                val (chs, pgs) = chPg
                for (fmt in formats) {
                    print("Running Warmup [$scName - $fmt]... ")
                    runSingleTrial(scName, fmt, 1, "warmup_P1", numSrc, chs, pgs)
                    runSingleTrial(scName, fmt, 2, "warmup_P2", numSrc, chs, pgs)
                    println("done.")

                    // ABBA sequence: A1 (P1), B1 (P2), B2 (P2), A2 (P1)
                    print("Running ABBA [$scName - $fmt]... ")
                    val rA1 = runSingleTrial(scName, fmt, 1, "A1", numSrc, chs, pgs)
                    val rB1 = runSingleTrial(scName, fmt, 2, "B1", numSrc, chs, pgs)
                    val rB2 = runSingleTrial(scName, fmt, 2, "B2", numSrc, chs, pgs)
                    val rA2 = runSingleTrial(scName, fmt, 1, "A2", numSrc, chs, pgs)
                    allResults.addAll(listOf(rA1, rB1, rB2, rA2))
                    println("done. A_avg=${(rA1.elapsedMs + rA2.elapsedMs) / 2}ms, B_avg=${(rB1.elapsedMs + rB2.elapsedMs) / 2}ms")
                }
            }

            val totalElapsedWallMs = (System.nanoTime() - startWallNs) / 1_000_000L
            println("\n=== All Trials Completed in ${totalElapsedWallMs}ms ===")

            val runNonce = UUID.randomUUID().toString()
            val classOrigin = ChaptersFilesProvider::class.java.protectionDomain.codeSource?.location?.toString() ?: "unknown"
            val jvmFlags = ManagementFactory.getRuntimeMXBean().inputArguments.joinToString(" ")

            val reportDir = File(System.getProperty("mobile.proof.report.dir")
                ?: File(".superpowers/sdd").canonicalPath)
            reportDir.mkdirs()

            allPassedOverall = allResults.all { it.outputIntegrityVerified && it.monotonicProgressVerified }

            val jsonReport = File(reportDir, "page-workers-result.json")
            val sbJson = StringBuilder()
            sbJson.append("{\n")
            sbJson.append("  \"runNonce\": \"$runNonce\",\n")
            sbJson.append("  \"artifactSha\": \"$artifactSha\",\n")
            sbJson.append("  \"classOrigin\": \"$classOrigin\",\n")
            sbJson.append("  \"jvmFlags\": \"$jvmFlags\",\n")
            sbJson.append("  \"timestampUtc\": \"${Instant.now()}\",\n")
            sbJson.append("  \"totalWallMs\": $totalElapsedWallMs,\n")
            sbJson.append("  \"trialsCount\": ${allResults.size},\n")
            sbJson.append("  \"allPassed\": $allPassedOverall,\n")
            sbJson.append("  \"isolationChecks\": {\n")
            validatedPaths.entries.forEachIndexed { i, (k, v) ->
                sbJson.append("    \"$k\": \"$v\"${if (i < validatedPaths.size - 1) "," else ""}\n")
            }
            sbJson.append("  },\n")
            sbJson.append("  \"trials\": [\n")
            allResults.forEachIndexed { idx, r ->
                sbJson.append("    {\n")
                sbJson.append("      \"scenario\": \"${r.scenario}\",\n")
                sbJson.append("      \"format\": \"${r.format}\",\n")
                sbJson.append("      \"pageWorkers\": ${r.pageWorkers},\n")
                sbJson.append("      \"runType\": \"${r.runType}\",\n")
                sbJson.append("      \"elapsedMs\": ${r.elapsedMs},\n")
                sbJson.append("      \"cpuTimeMs\": ${r.cpuTimeMs},\n")
                sbJson.append("      \"peakStreams\": ${r.peakStreams},\n")
                sbJson.append("      \"heapBeforeBytes\": ${r.heapBeforeBytes},\n")
                sbJson.append("      \"heapSettledBytes\": ${r.heapSettledBytes},\n")
                sbJson.append("      \"vmRssBeforeKb\": ${r.vmRssBeforeKb},\n")
                sbJson.append("      \"vmRssSettledKb\": ${r.vmRssSettledKb},\n")
                sbJson.append("      \"threadsBefore\": ${r.threadCountBefore},\n")
                sbJson.append("      \"peakThreads\": ${r.peakThreads},\n")
                sbJson.append("      \"settledThreads\": ${r.settledThreads},\n")
                sbJson.append("      \"settledFds\": ${r.settledFds},\n")
                sbJson.append("      \"settledClaims\": ${r.settledClaims},\n")
                sbJson.append("      \"settledPermits\": ${r.settledPermits},\n")
                sbJson.append("      \"settledStreams\": ${r.settledStreams},\n")
                sbJson.append("      \"integrityVerified\": ${r.outputIntegrityVerified},\n")
                sbJson.append("      \"monotonicProgressVerified\": ${r.monotonicProgressVerified}\n")
                sbJson.append("    }${if (idx < allResults.size - 1) "," else ""}\n")
            }
            sbJson.append("  ]\n")
            sbJson.append("}\n")
            jsonReport.writeText(sbJson.toString())
            println("Compact JSON report written to: $jsonReport")

        } catch (t: Throwable) {
            System.err.println("MainNativeProbe execution failed with exception:")
            t.printStackTrace()
            allPassedOverall = false
        } finally {
            try {
                // Quiescence cleanup before exit
                if (bootstrapped) runBlocking { DownloadManager.clear() }
            } catch (_: Throwable) {}
            println("MainNativeProbe exiting with code: ${if (allPassedOverall) 0 else 1}")
            exitProcess(if (allPassedOverall) 0 else 1)
        }
    }
}
