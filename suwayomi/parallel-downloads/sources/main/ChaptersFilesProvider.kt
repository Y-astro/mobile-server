package suwayomi.tachidesk.manga.impl.download.fileProvider

import eu.kanade.tachiyomi.source.local.metadata.COMIC_INFO_FILE
import io.github.oshai.kotlinlogging.KotlinLogging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.sample
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import libcore.net.MimeUtils
import org.apache.commons.compress.archivers.zip.ZipArchiveEntry
import org.jetbrains.exposed.v1.core.eq
import org.jetbrains.exposed.v1.jdbc.selectAll
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import org.jetbrains.exposed.v1.jdbc.update
import suwayomi.tachidesk.manga.impl.Page
import suwayomi.tachidesk.manga.impl.chapter.getChapterDownloadReady
import suwayomi.tachidesk.manga.impl.download.DownloaderNetworkBudget
import suwayomi.tachidesk.manga.impl.download.model.DownloadQueueItem
import suwayomi.tachidesk.manga.impl.util.KoreaderHelper
import suwayomi.tachidesk.manga.impl.util.createComicInfoFile
import suwayomi.tachidesk.manga.impl.util.getChapterCachePath
import suwayomi.tachidesk.manga.impl.util.getChapterCbzPath
import suwayomi.tachidesk.manga.impl.util.getChapterDownloadPath
import suwayomi.tachidesk.manga.impl.util.storage.ImageResponse
import suwayomi.tachidesk.manga.model.table.ChapterTable
import suwayomi.tachidesk.manga.model.table.MangaTable
import java.io.File
import java.io.InputStream
import java.util.concurrent.atomic.AtomicInteger

sealed class FileType {
    data class RegularFile(
        val file: File,
    ) : FileType()

    data class ZipFile(
        val entry: ZipArchiveEntry,
    ) : FileType()

    fun getName(): String =
        when (this) {
            is RegularFile -> {
                this.file.name
            }

            is ZipFile -> {
                this.entry.name
            }
        }

    fun getExtension(): String =
        when (this) {
            is RegularFile -> {
                this.file.extension
            }

            is ZipFile -> {
                this.entry.name.substringAfterLast(".")
            }
        }
}

/*
* Base class for downloaded chapter files provider, example: Folder, Archive
*/
abstract class ChaptersFilesProvider<Type : FileType>(
    val mangaId: Int,
    val chapterId: Int,
) : DownloadedFilesProvider {
    companion object {
        const val DEFAULT_PAGE_WORKERS = 2

        @Volatile
        internal var pageWorkersOverride: Int? = null

        internal fun resetForTest() {
            pageWorkersOverride = null
        }
    }

    protected val logger = KotlinLogging.logger {}

    protected abstract suspend fun getImageFiles(): List<Type>

    protected abstract suspend fun getImageInputStream(image: Type): InputStream

    suspend fun getImageImpl(index: Int): Pair<InputStream, String> {
        val images = getImageFiles().filter { it.getName() != COMIC_INFO_FILE }.sortedBy { it.getName() }

        if (images.isEmpty()) {
            throw NoSuchElementException("no downloaded images found")
        }

        val image = images[index]
        val imageFileType = image.getExtension()

        return Pair(getImageInputStream(image).buffered(), MimeUtils.guessMimeTypeFromExtension(imageFileType) ?: "image/$imageFileType")
    }

    suspend fun getImageCount(): Int = getImageFiles().filter { it.getName() != COMIC_INFO_FILE }.size

    override suspend fun getImage(): RetrieveFile1Args<Int> = RetrieveFile1Args(::getImageImpl)

    /**
     * Extract the existing download to the base download folder (see [getChapterDownloadPath])
     */
    protected abstract suspend fun extractExistingDownload()

    protected abstract suspend fun handleSuccessfulDownload()

    @OptIn(FlowPreview::class)
    private suspend fun downloadImpl(
        download: DownloadQueueItem,
        scope: CoroutineScope,
        step: suspend (DownloadQueueItem?, Boolean) -> Unit,
    ): Boolean {
        val existingDownloadPageCount =
            try {
                getImageCount()
            } catch (_: Exception) {
                0
            }
        val pageCount = download.pageCount

        check(pageCount > 0) { "pageCount must be greater than 0 - ChapterForDownload#getChapterDownloadReady not called" }
        check(existingDownloadPageCount == 0 || existingDownloadPageCount == pageCount) {
            "existingDownloadPageCount must be 0 or equal to pageCount - ChapterForDownload#getChapterDownloadReady not called"
        }

        val doesUnrecognizedDownloadExist = existingDownloadPageCount == pageCount
        if (doesUnrecognizedDownloadExist) {
            download.progress = 1f
            step(download, false)

            return true
        }

        extractExistingDownload()

        val finalDownloadFolder = getChapterDownloadPath(mangaId, chapterId)

        val cacheChapterDir = getChapterCachePath(mangaId, chapterId)
        val downloadCacheFolder = File(cacheChapterDir)
        downloadCacheFolder.mkdirs()

        val pageProgress = FloatArray(pageCount)
        val pageCompleted = BooleanArray(pageCount)
        val progressLock = Any()
        var lastReportedProgress = 0f

        fun updateProgress(pageNum: Int, pageFraction: Float, isComplete: Boolean) {
            synchronized(progressLock) {
                if (pageCompleted[pageNum]) return
                if (isComplete) {
                    pageCompleted[pageNum] = true
                    pageProgress[pageNum] = 1f
                } else {
                    val clamped = pageFraction.coerceIn(0f, 1f)
                    if (clamped > pageProgress[pageNum]) {
                        pageProgress[pageNum] = clamped
                    }
                }
                var sum = 0f
                for (p in 0 until pageCount) {
                    sum += pageProgress[p]
                }
                val aggregated = (sum / pageCount).coerceIn(0f, 1f)
                if (aggregated > lastReportedProgress) {
                    lastReportedProgress = aggregated
                    download.progress = aggregated
                }
            }
        }

        for (pageNum in 0 until pageCount) {
            val fileName = Page.getPageName(pageNum, pageCount) // might have to change this to index stored in database
            val pageExistsInFinalDownloadFolder = ImageResponse.findFileNameStartingWith(finalDownloadFolder, fileName) != null
            val pageExistsInCacheDownloadFolder = ImageResponse.findFileNameStartingWith(cacheChapterDir, fileName) != null
            if (pageExistsInFinalDownloadFolder || pageExistsInCacheDownloadFolder) {
                updateProgress(pageNum, 1f, isComplete = true)
            }
        }

        if (lastReportedProgress > 0f) {
            step(download, false)
        }

        val effectiveWorkerLimit = (pageWorkersOverride ?: DEFAULT_PAGE_WORKERS).coerceAtLeast(1)
        val workerCount = minOf(effectiveWorkerLimit, pageCount)
        val nextPageIndex = AtomicInteger(0)

        coroutineScope {
            for (w in 0 until workerCount) {
                launch {
                    val workerScope = this
                    while (isActive) {
                        val pageNum = nextPageIndex.getAndIncrement()
                        if (pageNum >= pageCount) break

                        val isAlreadyDone = synchronized(progressLock) { pageCompleted[pageNum] }
                        if (isAlreadyDone) {
                            continue
                        }

                        val fileName = Page.getPageName(pageNum, pageCount) // might have to change this to index stored in database
                        var pageProgressJob: Job? = null
                        try {
                            DownloaderNetworkBudget.withPermit {
                                Page.getPageImageDownload(
                                    mangaId = download.mangaId,
                                    chapterId = download.chapterId,
                                    index = pageNum,
                                    downloadCacheFolder = downloadCacheFolder,
                                    fileName = fileName,
                                ) { flow ->
                                    pageProgressJob =
                                        flow
                                            .sample(100)
                                            .distinctUntilChanged()
                                            .onEach { percent ->
                                                updateProgress(pageNum, percent.toFloat() * 0.01f, isComplete = false)
                                                step(
                                                    null,
                                                    false,
                                                ) // don't throw on canceled download here since we can't do anything
                                            }.launchIn(workerScope)
                                }
                            }
                        } finally {
                            // always cancel the page progress job even if it throws an exception to avoid memory leaks
                            pageProgressJob?.cancelAndJoin()
                        }

                        // TODO: retry on error with 2,4,8 seconds of wait
                        updateProgress(pageNum, 1f, isComplete = true)
                        step(download, false)
                    }
                }
            }
        }

        check(pageCompleted.all { it }) { "Not all pages completed successfully for chapter $chapterId" }

        createComicInfoFile(
            downloadCacheFolder.toPath(),
            transaction {
                MangaTable.selectAll().where { MangaTable.id eq mangaId }.first()
            },
            transaction {
                ChapterTable.selectAll().where { ChapterTable.id eq chapterId }.first()
            },
        )

        handleSuccessfulDownload()

        // Calculate and save Koreader hash for CBZ files
        val chapterFile = File(getChapterCbzPath(mangaId, chapterId))
        if (chapterFile.exists()) {
            val koreaderHash = KoreaderHelper.hashContents(chapterFile)
            if (koreaderHash != null) {
                transaction {
                    ChapterTable.update({ ChapterTable.id eq chapterId }) {
                        it[ChapterTable.koreaderHash] = koreaderHash
                    }
                }
            }
        }

        File(cacheChapterDir).deleteRecursively()

        return true
    }

    /**
     * This function should never be called without calling [getChapterDownloadReady] beforehand.
     */
    override fun download(): FileDownload3Args<DownloadQueueItem, CoroutineScope, suspend (DownloadQueueItem?, Boolean) -> Unit> =
        FileDownload3Args(::downloadImpl)

    abstract override suspend fun delete(): Boolean

    abstract suspend fun getAsArchiveStream(): Pair<InputStream, Long>

    abstract suspend fun getArchiveSize(): Long
}
