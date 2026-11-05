package suwayomi.tachidesk.manga.impl.download

/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import java.util.concurrent.atomic.AtomicInteger

object DownloaderNetworkBudget {
    const val DEFAULT_MAX_PERMITS = 6

    private var semaphore = Semaphore(DEFAULT_MAX_PERMITS)
    val activeUnits = AtomicInteger(0)
    val peakActiveUnits = AtomicInteger(0)

    val availablePermits: Int
        get() = semaphore.availablePermits

    suspend fun <T> withPermit(action: suspend () -> T): T =
        semaphore.withPermit {
            val current = activeUnits.incrementAndGet()
            peakActiveUnits.updateAndGet { peak -> if (current > peak) current else peak }
            try {
                action()
            } finally {
                activeUnits.decrementAndGet()
            }
        }

    fun reset(permits: Int = DEFAULT_MAX_PERMITS) {
        semaphore = Semaphore(permits)
        activeUnits.set(0)
        peakActiveUnits.set(0)
    }
}
