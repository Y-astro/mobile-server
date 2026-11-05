package suwayomi.tachidesk.manga.impl.download

/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import eu.kanade.tachiyomi.source.local.LocalSource
import io.github.oshai.kotlinlogging.KLogger
import io.github.oshai.kotlinlogging.KotlinLogging
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import org.jetbrains.exposed.v1.jdbc.update
import suwayomi.tachidesk.manga.impl.ChapterDownloadHelper
import suwayomi.tachidesk.manga.impl.chapter.getChapterDownloadReadyById
import suwayomi.tachidesk.manga.impl.download.model.DownloadQueueItem
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Downloading
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Error
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Finished
import suwayomi.tachidesk.manga.impl.download.model.DownloadState.Queued
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdate
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType.ERROR
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType.FINISHED
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType.PAUSED
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType.PROGRESS
import suwayomi.tachidesk.manga.impl.download.model.DownloadUpdateType.STOPPED
import suwayomi.tachidesk.manga.model.table.ChapterTable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList

class Downloader(
    private val scope: CoroutineScope,
    val sourceId: Long,
    private val downloadQueue: CopyOnWriteArrayList<DownloadQueueItem>,
    private val notifier: (immediate: Boolean, download: DownloadUpdate?) -> Unit,
    private val onComplete: () -> Unit,
    private val onDownloadFinished: () -> Unit,
) {
    companion object {
        const val MAX_RETRIES = 3
        const val MAX_ACTIVE_CHAPTERS_PER_SOURCE = 2
    }

    private val logger = KotlinLogging.logger("${Downloader::class.java.name} source($sourceId)")

    private val activeChapterJobs = ConcurrentHashMap<Int, Job>()

    val isActive: Boolean
        get() = activeChapterJobs.isNotEmpty()

    val activeChapterCount: Int
        get() = activeChapterJobs.size

    val activeChapterIds: Set<Int>
        get() = activeChapterJobs.keys.toSet()

    class StopDownloadException : Exception("Cancelled download")

    class PauseDownloadException : Exception("Pause download")

    class EmptyChapterException : Exception("Chapter does not have any pages to download")

    private fun notify(
        immediate: Boolean,
        update: DownloadUpdate? = null,
    ) {
        val isDownloadCanceled = !downloadQueue.contains(update?.downloadQueueItem)
        if (isDownloadCanceled) {
            return
        }

        notifier(immediate, update)
    }

    private fun notify(
        immediate: Boolean,
        type: DownloadUpdateType,
        download: DownloadQueueItem?,
    ) {
        notify(immediate, download?.let { DownloadUpdate(type, download, downloadQueue.indexOf(download)) })
    }

    private fun isAdmitted(download: DownloadQueueItem): Boolean {
        if (download !in downloadQueue) {
            return false
        }
        val eligibleSourceDownloads = downloadQueue.filter { it.sourceId == sourceId && it.state != Error }
        val admitted = eligibleSourceDownloads.take(MAX_ACTIVE_CHAPTERS_PER_SOURCE)
        return download in admitted
    }

    private suspend fun step(
        type: DownloadUpdateType,
        download: DownloadQueueItem?,
        immediate: Boolean,
    ) {
        notify(immediate, type, download)
        currentCoroutineContext().ensureActive()
        if (download != null) {
            if (download !in downloadQueue) {
                throw StopDownloadException()
            }
            if (!isAdmitted(download)) {
                throw PauseDownloadException()
            }
        }
    }

    fun start() {
        onComplete()
    }

    fun startChapter(download: DownloadQueueItem): Job? {
        if (activeChapterJobs.containsKey(download.chapterId)) {
            return activeChapterJobs[download.chapterId]
        }
        if (activeChapterJobs.size >= MAX_ACTIVE_CHAPTERS_PER_SOURCE) {
            return null
        }

        val job =
            scope.launch {
                runChapter(download)
            }
        activeChapterJobs[download.chapterId] = job
        job.invokeOnCompletion { cause ->
            activeChapterJobs.remove(download.chapterId)
            if (cause !is CancellationException) {
                logger.debug { "completed chapter ${download.chapterId}" }
                onComplete()
            }
        }
        logger.debug { "started chapter ${download.chapterId} (active: ${activeChapterJobs.size})" }
        notify(false)
        return job
    }

    suspend fun stop() {
        val jobs = activeChapterJobs.values.toList()
        jobs.forEach { it.cancel() }
        jobs.forEach { it.join() }
        activeChapterJobs.clear()
        logger.debug { "stopped" }
    }

    suspend fun stopChapter(chapterId: Int) {
        val job = activeChapterJobs.remove(chapterId)
        job?.cancelAndJoin()
    }

    private fun finishDownload(
        logger: KLogger,
        download: DownloadQueueItem,
    ) {
        notify(true, FINISHED, download)
        downloadQueue -= download
        onDownloadFinished()
        logger.debug { "finished" }
    }

    private suspend fun runChapter(download: DownloadQueueItem) {
        val logContext = "${logger.name} - downloadChapter($download)"
        val downloadLogger = KotlinLogging.logger(logContext)

        // Skip local source downloads
        if (sourceId == LocalSource.ID) {
            finishDownload(downloadLogger, download)
            return
        }

        downloadLogger.debug { "start" }

        // handle cases where the downloader was stopped before the finished download could be removed from the queue
        if (download.state == Finished) {
            finishDownload(downloadLogger, download)
            return
        }

        try {
            download.state = Downloading
            step(PROGRESS, download, true)

            val chapter =
                DownloaderNetworkBudget.withPermit {
                    getChapterDownloadReadyById(download.chapterId)
                }

            if (chapter.pageCount <= 0) {
                throw EmptyChapterException()
            }

            download.pageCount = chapter.pageCount

            ChapterDownloadHelper.download(download.mangaId, download.chapterId, download, scope) { downloadChapter, immediate ->
                step(PROGRESS, downloadChapter, immediate)
            }
            download.state = Finished
            transaction {
                ChapterTable.update(
                    { (ChapterTable.id eq download.chapterId) },
                ) {
                    it[isDownloaded] = true
                }
            }
            finishDownload(downloadLogger, download)
        } catch (e: CancellationException) {
            downloadLogger.debug { "Downloader was stopped" }
            if (download in downloadQueue && download.state == Downloading) {
                download.state = Queued
            }
            notify(false, STOPPED, download)
        } catch (e: StopDownloadException) {
            downloadLogger.debug { "Download was dequeued" }
        } catch (e: PauseDownloadException) {
            downloadLogger.debug { "paused" }
            download.state = Queued
            notify(false, PAUSED, download)
        } catch (e: EmptyChapterException) {
            downloadLogger.warn(e) { "failed due to" }
            download.tries = MAX_RETRIES
            download.state = Error
            notify(false, ERROR, download)
        } catch (e: Exception) {
            downloadLogger.warn(e) { "failed due to" }
            download.tries++
            download.state = Queued
            if (download.tries >= MAX_RETRIES) {
                download.state = Error
                notify(false, ERROR, download)
            }
        }
    }
}
