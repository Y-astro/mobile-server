package suwayomi.tachidesk.manga.impl.download

/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import android.app.Application
import android.content.Context
import io.github.oshai.kotlinlogging.KotlinLogging
import io.javalin.websocket.WsContext
import io.javalin.websocket.WsMessageContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.onStart
import kotlinx.coroutines.flow.sample
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import org.jetbrains.exposed.v1.core.and
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.core.inList
import org.jetbrains.exposed.v1.jdbc.select
import org.jetbrains.exposed.v1.jdbc.selectAll
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import suwayomi.tachidesk.graphql.types.ChapterDownloadReorder
import suwayomi.tachidesk.manga.impl.download.Downloader.Companion.MAX_ACTIVE_CHAPTERS_PER_SOURCE
import suwayomi.tachidesk.manga.impl.download.Downloader.Companion.MAX_RETRIES
import suwayomi.tachidesk.manga.impl.download.model.DownloadChapter
import suwayomi.tachidesk.manga.impl.download.model.DownloadQueueItem
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Downloading
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Error
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Finished
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Queued
import suwayomi.tachidesk.manga.impl.download.model.DownloadStatus
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdate
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdates
import suwayomi.tachidesk.manga.impl.download.model.OldDownloadStatus
import suwayomi.tachidesk.manga.impl.download.model.Status
import suwayomi.tachidesk.manga.model.dataclass.ChapterDataClass
import suwayomi.tachidesk.manga.model.dataclass.MangaDataClass
import suwayomi.tachidesk.manga.model.table.ChapterTable
import suwayomi.tachidesk.manga.model.table.MangaTable
import suwayomi.tachidesk.manga.model.table.toDataClass
import suwayomi.tachidesk.server.serverConfig
import uy.kohesive.injekt.Injekt
import uy.kohesive.injekt.api.get
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.reflect.jvm.jvmName
import kotlin.time.Duration.Companion.seconds

private val logger = KotlinLogging.logger {}

internal data class ChapterClaim(
    val chapterId: Int,
    val sourceId: Long,
    val downloadItem: DownloadQueueItem,
    val job: Job,
)

@OptIn(FlowPreview::class)
object DownloadManager {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val clients = ConcurrentHashMap<String, WsContext>()
    private val downloadQueue = CopyOnWriteArrayList<DownloadQueueItem>()
    private val downloadUpdates = CopyOnWriteArraySet<DownloadUpdate>()
    private val downloaders = ConcurrentHashMap<Long, Downloader>()

    private const val DOWNLOAD_QUEUE_KEY = "downloadQueueKey"
    private val sharedPreferences =
        Injekt.get<Application>().getSharedPreferences(DownloadManager::class.jvmName, Context.MODE_PRIVATE)

    private fun loadDownloadQueue(): List<Int> =
        sharedPreferences
            .getStringSet(DOWNLOAD_QUEUE_KEY, emptySet())
            ?.mapNotNull {
                it.toInt()
            }.orEmpty()

    private fun saveDownloadQueue() {
        sharedPreferences
            .edit()
            .putStringSet(DOWNLOAD_QUEUE_KEY, downloadQueue.map { it.chapterId.toString() }.toSet())
            .commit()
    }

    private fun triggerSaveDownloadQueue() {
        scope.launch { saveQueueFlow.emit(Unit) }
    }

    private fun handleDownloadUpdate(
        immediate: Boolean,
        download: DownloadUpdate? = null,
    ) {
        notifyAllClients(immediate, listOfNotNull(download))
    }

    fun restoreAndResumeDownloads() {
        scope.launch {
            logger.debug { "restoreAndResumeDownloads: Restore download queue..." }
            enqueue(EnqueueInput(loadDownloadQueue()))

            if (downloadQueue.size > 0) {
                logger.info { "restoreAndResumeDownloads: Restored download queue, starting downloads..." }
            }
        }
    }

    fun addClient(ctx: WsContext) {
        clients[ctx.sessionId()] = ctx
    }

    fun removeClient(ctx: WsContext) {
        clients.remove(ctx.sessionId())
    }

    fun notifyClient(ctx: WsContext) {
        ctx.send(
            getStatus(),
        )
    }

    fun handleRequest(ctx: WsMessageContext) {
        when (ctx.message()) {
            "STATUS" -> {
                notifyClient(ctx)
            }

            else -> {
                ctx.send(
                    """
                        |Invalid command.
                        |Supported commands are:
                        |    - STATUS
                        |       sends the current download status
                        |
                    """.trimMargin(),
                )
            }
        }
    }

    private val notifyFlow = MutableSharedFlow<Unit>(extraBufferCapacity = 1, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    @Deprecated("Replaced with updatesFlow", replaceWith = ReplaceWith("updatesFlow"))
    private val statusFlow = MutableSharedFlow<DownloadStatus>()

    @Deprecated("Replaced with updates", replaceWith = ReplaceWith("updates"))
    val status = statusFlow.onStart { emit(getStatus()) }

    private val updatesFlow = MutableSharedFlow<DownloadUpdates>()
    val updates = updatesFlow.onStart { emit(getDownloadUpdates(addInitial = true)) }

    init {
        scope.launch {
            notifyFlow.sample(1.seconds).collect {
                notifyAllClients(immediate = true, gqlEmit = true)
            }
        }
    }

    private val saveQueueFlow = MutableSharedFlow<Unit>(extraBufferCapacity = 1, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    init {
        saveQueueFlow.onEach { saveDownloadQueue() }.launchIn(scope)
    }

    private fun notifyAllClients(
        immediate: Boolean = false,
        downloads: List<DownloadUpdate> = emptyList(),
        gqlEmit: Boolean = false,
    ) {
        val outdatedUpdates =
            downloadUpdates.filter { update ->
                downloads.any { download ->
                    download.downloadQueueItem.chapterId ==
                        update.downloadQueueItem.chapterId
                }
            }
        downloadUpdates.removeAll(outdatedUpdates.toSet())
        downloadUpdates.addAll(downloads)

        if (gqlEmit) {
            val updates = getDownloadUpdates()

            downloadUpdates.clear()

            scope.launch {
                updatesFlow.emit(updates)
            }
        }

        if (immediate) {
            val status = getStatus()
            scope.launch {
                statusFlow.emit(status)
                if (clients.isNotEmpty()) {
                    val status = getOldStatus(status)
                    clients.forEach {
                        it.value.send(status)
                    }
                }
            }

            return
        }

        scope.launch {
            notifyFlow.emit(Unit)
        }
    }

    fun getStatus(): DownloadStatus =
        DownloadStatus(
            if (activeClaims.isNotEmpty()) {
                Status.Started
            } else {
                Status.Stopped
            },
            downloadQueue.toList(),
        )

    fun getOldStatus(status: DownloadStatus): OldDownloadStatus =
        OldDownloadStatus(
            status.status,
            run {
                val items = status.queue
                val mangaIds = items.map { it.mangaId }.toSet()
                val chapterIds = items.map { it.chapterId }.toSet()
                transaction {
                    val mangas =
                        MangaTable
                            .selectAll()
                            .where { MangaTable.id inList mangaIds }
                            .associateBy({ it[MangaTable.id].value }, { MangaTable.toDataClass(it) })
                    val chapters =
                        ChapterTable
                            .selectAll()
                            .where { ChapterTable.id inList chapterIds }
                            .associateBy({ it[ChapterTable.id].value }, { ChapterTable.toDataClass(it) })
                    items.mapNotNull {
                        DownloadChapter(
                            it.chapterIndex,
                            it.mangaId,
                            chapters[it.chapterId] ?: return@mapNotNull null,
                            mangas[it.mangaId] ?: return@mapNotNull null,
                            it.state,
                            it.progress,
                            it.tries,
                        )
                    }
                }
            },
        )

    private fun getDownloadUpdates(addInitial: Boolean = false): DownloadUpdates =
        DownloadUpdates(
            if (activeClaims.isNotEmpty()) {
                Status.Started
            } else {
                Status.Stopped
            },
            downloadUpdates.toList(),
            if (addInitial) downloadQueue.toList() else null,
        )

    const val MAX_ACTIVE_CHAPTERS_GLOBAL = 6
    private val isRunning = AtomicBoolean(false)
    private val admissionLock = Any()
    internal val activeClaims = ConcurrentHashMap<Int, ChapterClaim>()

    @Volatile
    internal var maxChaptersPerSourceOverride: Int? = null

    @Volatile
    internal var maxGlobalActiveOverride: Int? = null

    val maxChaptersPerSource: Int
        get() = maxChaptersPerSourceOverride ?: MAX_ACTIVE_CHAPTERS_PER_SOURCE

    val maxGlobalActive: Int
        get() = maxGlobalActiveOverride ?: MAX_ACTIVE_CHAPTERS_GLOBAL

    fun isSourceActive(sourceId: Long): Boolean =
        activeClaims.values.any { it.sourceId == sourceId }

    fun activeChapterCountForSource(sourceId: Long): Int =
        activeClaims.values.count { it.sourceId == sourceId }

    val activeChapterCount: Int
        get() = activeClaims.size

    val activeChapterIds: Set<Int>
        get() = activeClaims.keys.toSet()

    private fun admittedSourceIds(): Set<Long> =
        downloadQueue
            .filter { it.state != Error || it.tries < MAX_RETRIES }
            .map { it.sourceId }
            .distinct()
            .take(serverConfig.maxSourcesInParallel.value.coerceIn(1, MAX_ACTIVE_CHAPTERS_GLOBAL))
            .toSet()

    fun isAdmitted(download: DownloadQueueItem): Boolean {
        if (!isRunning.get()) return false
        if (download !in downloadQueue) return false
        if (download.sourceId !in admittedSourceIds()) return false
        val eligibleSourceDownloads = downloadQueue.filter { it.sourceId == download.sourceId && it.state != Error }
        val admitted = eligibleSourceDownloads.take(maxChaptersPerSource)
        return download in admitted
    }

    private val downloaderWatch = MutableSharedFlow<Unit>(extraBufferCapacity = 1, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    init {
        serverConfig.subscribeTo(serverConfig.maxSourcesInParallel, { maxSourcesInParallel ->
            logger.debug { "Max sources in parallel changed to $maxSourcesInParallel" }
            refreshDownloaders()
        })

        scope.launch {
            downloaderWatch.sample(1.seconds).collect {
                val runningDownloaders = downloaders.values.filter { it.isActive }
                val currentActiveClaims = activeClaims.size
                val availableDownloads = downloadQueue.filter { it.state != Error }

                logger.info {
                    "Running downloaders: ${runningDownloaders.size}, active chapters: $currentActiveClaims, " +
                        "Queued: ${availableDownloads.size}, " +
                        "Failed: ${downloadQueue.size - availableDownloads.size}"
                }

                admitDownloads()
            }
        }
    }

    private fun admitDownloads() {
        if (!isRunning.get()) return

        synchronized(admissionLock) {
            if (!isRunning.get()) return

            val maxGlobal = maxGlobalActive
            val maxPerSource = maxChaptersPerSource

            val activeClaimsPerSource = mutableMapOf<Long, Int>()
            for (claim in activeClaims.values) {
                activeClaimsPerSource[claim.sourceId] = (activeClaimsPerSource[claim.sourceId] ?: 0) + 1
            }

            var freeGlobalSlots = (maxGlobal - activeClaims.size).coerceAtLeast(0)
            if (freeGlobalSlots <= 0) return

            val allowedSources = admittedSourceIds()
            val eligibleItems =
                downloadQueue.filter {
                    it.sourceId in allowedSources &&
                    (it.state == Queued || it.state == Finished || (it.state == Error && it.tries < MAX_RETRIES)) &&
                        !activeClaims.containsKey(it.chapterId)
                }

            if (eligibleItems.isEmpty()) return

            val toAdmit = mutableListOf<DownloadQueueItem>()

            // Priority 1: Ready sources needing their FIRST lane (active count == 0)
            for (item in eligibleItems) {
                if (freeGlobalSlots <= 0) break
                val count = activeClaimsPerSource.getOrDefault(item.sourceId, 0)
                if (count == 0) {
                    toAdmit.add(item)
                    activeClaimsPerSource[item.sourceId] = 1
                    freeGlobalSlots--
                }
            }

            // Priority 2: Ready sources needing their SECOND lane (active count < maxPerSource)
            if (freeGlobalSlots > 0) {
                for (item in eligibleItems) {
                    if (freeGlobalSlots <= 0) break
                    if (item in toAdmit) continue
                    val count = activeClaimsPerSource.getOrDefault(item.sourceId, 0)
                    if (count < maxPerSource) {
                        toAdmit.add(item)
                        activeClaimsPerSource[item.sourceId] = count + 1
                        freeGlobalSlots--
                    }
                }
            }

            for (item in toAdmit) {
                val downloader = getDownloader(item.sourceId)
                lateinit var claim: ChapterClaim
                val job =
                    scope.launch(start = CoroutineStart.LAZY) {
                        downloader.runChapter(item)
                    }
                claim = ChapterClaim(item.chapterId, item.sourceId, item, job)
                activeClaims[item.chapterId] = claim
                job.invokeOnCompletion {
                    activeClaims.remove(item.chapterId, claim)
                    refreshDownloaders()
                }
                job.start()
            }

            if (toAdmit.isNotEmpty()) {
                notifyAllClients()
            }
        }
    }

    private fun refreshDownloaders() {
        admitDownloads()
        scope.launch {
            downloaderWatch.emit(Unit)
        }
    }

    private fun getDownloader(sourceId: Long) =
        downloaders.getOrPut(sourceId) {
            Downloader(
                scope = scope,
                sourceId = sourceId,
                downloadQueue = downloadQueue,
                notifier = ::handleDownloadUpdate,
                onComplete = ::refreshDownloaders,
                onDownloadFinished = ::triggerSaveDownloadQueue,
            )
        }

    fun enqueueWithChapterIndex(
        mangaId: Int,
        chapterIndex: Int,
    ) {
        val chapter =
            transaction {
                ChapterTable
                    .select(ChapterTable.id)
                    .where { (ChapterTable.manga eq mangaId) and (ChapterTable.sourceOrder eq chapterIndex) }
                    .first()
            }
        enqueue(EnqueueInput(chapterIds = listOf(chapter[ChapterTable.id].value)))
    }

    @Serializable
    data class EnqueueInput(
        val chapterIds: List<Int>?,
    )

    fun enqueue(input: EnqueueInput) {
        if (input.chapterIds.isNullOrEmpty()) return

        val chapters =
            transaction {
                (ChapterTable innerJoin MangaTable)
                    .selectAll()
                    .where { ChapterTable.id inList input.chapterIds }
                    .orderBy(ChapterTable.manga)
                    .orderBy(ChapterTable.sourceOrder)
                    .toList()
            }

        val mangas =
            transaction {
                chapters
                    .distinctBy { chapter -> chapter[MangaTable.id] }
                    .map { MangaTable.toDataClass(it) }
                    .associateBy { it.id }
            }

        val inputPairs =
            transaction {
                chapters.map {
                    Pair(
                        mangas[it[ChapterTable.manga].value]!!,
                        ChapterTable.toDataClass(it),
                    )
                }
            }

        addMultipleToQueue(inputPairs)
    }

    private fun addMultipleToQueue(inputs: List<Pair<MangaDataClass, ChapterDataClass>>) {
        val size = downloadQueue.size
        val addedChapters = inputs.mapNotNull { addToQueue(it.first, it.second) }
        if (addedChapters.isNotEmpty()) {
            start()
            notifyAllClients(
                false,
                addedChapters.mapIndexed { index, item ->
                    DownloadUpdate(
                        DownloadUpdateType.QUEUED,
                        item,
                        size + index,
                    )
                },
            )
            triggerSaveDownloadQueue()
        }
    }

    private fun addToQueue(
        manga: MangaDataClass,
        chapter: ChapterDataClass,
    ): DownloadQueueItem? {
        val downloadChapter = downloadQueue.firstOrNull { it.chapterId == chapter.id }

        val addToQueue = downloadChapter == null
        if (addToQueue) {
            val newDownloadChapter =
                DownloadQueueItem(
                    chapter.id,
                    chapter.index,
                    manga.id,
                    manga.sourceId.toLong(),
                    downloadQueue.size,
                )
            downloadQueue.add(newDownloadChapter)
            logger.debug { "Added chapter ${chapter.id} to download queue ($newDownloadChapter)" }
            return newDownloadChapter
        }

        val retryDownload = downloadChapter.state == Error
        if (retryDownload) {
            logger.debug { "Chapter ${chapter.id} download failed, retry download ($downloadChapter)" }

            downloadChapter.state = Queued
            downloadChapter.progress = 0f

            return downloadChapter
        }

        logger.debug { "Chapter ${chapter.id} already present in queue ($downloadChapter)" }
        return null
    }

    fun dequeue(input: EnqueueInput) {
        if (input.chapterIds.isNullOrEmpty()) return
        dequeue(downloadQueue.filter { it.chapterId in input.chapterIds }.toSet())
    }

    fun dequeue(
        chapterIndex: Int,
        mangaId: Int,
    ) {
        dequeue(downloadQueue.filter { it.mangaId == mangaId && it.chapterIndex == chapterIndex }.toSet())
    }

    fun dequeue(
        mangaIds: List<Int>,
        chaptersToIgnore: List<Int> = emptyList(),
    ) {
        dequeue(downloadQueue.filter { it.mangaId in mangaIds && it.chapterId !in chaptersToIgnore }.toSet())
    }

    private fun dequeue(chapterDownloads: Set<DownloadQueueItem>) {
        logger.debug { "dequeue ${chapterDownloads.size} chapters [${chapterDownloads.joinToString(separator = ", ") { "$it" }}]" }

        val jobsToCancel = mutableListOf<Job>()
        synchronized(admissionLock) {
            downloadQueue.removeAll(chapterDownloads)
            triggerSaveDownloadQueue()

            for (item in chapterDownloads) {
                val claim = activeClaims[item.chapterId]
                if (claim != null && claim.downloadItem === item) {
                    jobsToCancel.add(claim.job)
                }
            }
        }

        for (job in jobsToCancel) {
            job.cancel()
        }

        notifyAllClients(
            false,
            chapterDownloads.toList().map { DownloadUpdate(DownloadUpdateType.DEQUEUED, it, -1) },
        )
        refreshDownloaders()
    }

    fun reorder(
        chapterIndex: Int,
        mangaId: Int,
        to: Int,
    ) {
        val download =
            downloadQueue.find { it.mangaId == mangaId && it.chapterIndex == chapterIndex }
                ?: return

        reorder(listOf(ChapterDownloadReorder(download.chapterId, to)))
    }

    fun reorder(
        chapterId: Int,
        to: Int,
    ) {
        reorder(listOf(ChapterDownloadReorder(chapterId, to)))
    }

    fun reorder(reorders: List<ChapterDownloadReorder>) {
        val updates =
            reorders.mapNotNull {
                val download = reorder(it) ?: return@mapNotNull null

                DownloadUpdate(DownloadUpdateType.POSITION, download, it.to)
            }

        notifyAllClients(false, updates)
        triggerSaveDownloadQueue()
        refreshDownloaders()
    }

    private fun reorder(reorder: ChapterDownloadReorder): DownloadQueueItem? {
        val (chapterId, to) = reorder

        require(to >= 0) { "'to' must be over or equal to 0" }

        val download = downloadQueue.find { it.chapterId == chapterId } ?: return null

        logger.debug { "reorder download $download from ${downloadQueue.indexOf(download)} to $to" }

        downloadQueue -= download
        downloadQueue.add(to, download)

        return download
    }

    fun start() {
        logger.debug { "start" }
        isRunning.set(true)
        refreshDownloaders()
    }

    suspend fun stop() {
        logger.debug { "stop" }
        val jobsToJoin: List<Job>
        synchronized(admissionLock) {
            isRunning.set(false)
            jobsToJoin = activeClaims.values.map { it.job }.toList()
            for (job in jobsToJoin) {
                job.cancel()
            }
        }
        jobsToJoin.joinAll()
        notifyAllClients()
    }

    suspend fun stopSource(sourceId: Long) {
        val jobsToJoin: List<Job>
        synchronized(admissionLock) {
            val claimsToStop = activeClaims.values.filter { it.sourceId == sourceId }
            jobsToJoin = claimsToStop.map { it.job }
            for (job in jobsToJoin) {
                job.cancel()
            }
        }
        jobsToJoin.joinAll()
    }

    suspend fun stopChapter(chapterId: Int) {
        val jobToJoin: Job?
        synchronized(admissionLock) {
            val claim = activeClaims[chapterId]
            jobToJoin = claim?.job
            jobToJoin?.cancel()
        }
        jobToJoin?.join()
    }

    suspend fun clear() {
        logger.debug { "clear" }

        stop()
        val removedDownloads = downloadQueue.toList().map { DownloadUpdate(DownloadUpdateType.DEQUEUED, it, -1) }
        downloadQueue.clear()
        triggerSaveDownloadQueue()
        notifyAllClients(false, removedDownloads)
    }
}
