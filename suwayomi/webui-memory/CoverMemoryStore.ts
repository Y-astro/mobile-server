/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/.
 */

export interface CoverMemoryEntry {
    url: string;
    objectUrl: string;
    blob: Blob;
    bytes: number;
    fetchedAt: number;
    lastAccessed: number;
}

export interface CoverMemoryStoreOptions {
    maxStoreBytes?: number;
    maxImageBytes?: number;
    backgroundConcurrency?: number;
    fetchTimeoutMs?: number;
}

export class CoverMemoryStore {
    // 128 MiB default maximum memory
    public static readonly DEFAULT_MAX_STORE_BYTES = 128 * 1024 * 1024;
    // 16 MiB default per-image maximum
    public static readonly DEFAULT_MAX_IMAGE_BYTES = 16 * 1024 * 1024;
    // At most 2 concurrent low-priority background transfers
    public static readonly DEFAULT_BACKGROUND_CONCURRENCY = 2;
    // 15 seconds fetch timeout per background cover
    public static readonly DEFAULT_FETCH_TIMEOUT_MS = 15000;

    private readonly maxStoreBytes: number;
    private readonly maxImageBytes: number;
    private readonly backgroundConcurrency: number;
    private readonly fetchTimeoutMs: number;

    private readonly entries = new Map<string, CoverMemoryEntry>();
    private currentBytes: number = 0;
    private accessCounter: number = 0;
    private generation: number = 0;

    private backgroundQueue: string[] = [];
    private queuedSet = new Set<string>();
    private activeUrls: Set<string> | undefined;
    private activeForegroundTransfers: number = 0;
    private readonly abortControllers = new Map<string, AbortController>();

    private fetcher?: (url: string, signal: AbortSignal) => Promise<Blob>;

    constructor(options: CoverMemoryStoreOptions = {}) {
        this.maxStoreBytes = options.maxStoreBytes ?? CoverMemoryStore.DEFAULT_MAX_STORE_BYTES;
        this.maxImageBytes = options.maxImageBytes ?? CoverMemoryStore.DEFAULT_MAX_IMAGE_BYTES;
        this.backgroundConcurrency = options.backgroundConcurrency ?? CoverMemoryStore.DEFAULT_BACKGROUND_CONCURRENCY;
        this.fetchTimeoutMs = options.fetchTimeoutMs ?? CoverMemoryStore.DEFAULT_FETCH_TIMEOUT_MS;
    }

    public setFetcher(fetcher: (url: string, signal: AbortSignal) => Promise<Blob>): void {
        this.fetcher = fetcher;
    }

    /**
     * Checks if a URL is an allowed in-library manga thumbnail for the configured server.
     * Prevents cross-origin credential leaks and non-thumbnail/external caching.
     * Supports subpaths, e.g. baseUrl 'http://localhost:8080/suwayomi/' and '/suwayomi/api/v1/manga/162/thumbnail'.
     */
    public static isAllowedCoverUrl(url: string, configuredBaseUrl?: string): boolean {
        if (!url || typeof url !== 'string') {
            return false;
        }

        try {
            const fallbackOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8080';
            const baseParsed = configuredBaseUrl
                ? new URL(configuredBaseUrl, fallbackOrigin)
                : new URL(fallbackOrigin);

            const parsed = new URL(url, baseParsed.href);

            // Origin check: must match configured server origin strictly
            if (parsed.origin !== baseParsed.origin || parsed.username || parsed.password) {
                return false;
            }

            // Protocol check: only http or https
            if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
                return false;
            }

            // Path check: relative to base URL pathname
            let pathname = parsed.pathname;
            const basePath = baseParsed.pathname.replace(/\/+$/, '');
            if (basePath.length > 0) {
                if (!pathname.startsWith(`${basePath}/`)) return false;
                pathname = pathname.slice(basePath.length);
            }

            // Ensure leading slash for regex
            if (!pathname.startsWith('/')) {
                pathname = `/${pathname}`;
            }

            return /^(?:\/api\/v1)?\/manga\/\d+\/thumbnail\/?$/.test(pathname);
        } catch {
            return false;
        }
    }

    public has(url: string): boolean {
        return this.entries.has(url);
    }

    public get(url: string): CoverMemoryEntry | undefined {
        const entry = this.entries.get(url);
        if (entry) {
            entry.lastAccessed = ++this.accessCounter;
        }
        return entry;
    }

    public getObjectUrl(url: string): string | undefined {
        return this.get(url)?.objectUrl;
    }

    public put(url: string, blob: Blob, objectUrl?: string): string | null {
        if (!blob || !blob.size || blob.size > this.maxImageBytes || !blob.type.toLowerCase().startsWith('image/')) {
            return null;
        }

        // Never admit HTML or non-image content (e.g. auth redirect or error page)
        if (blob.type) {
            const lowerType = blob.type.toLowerCase();
            if (lowerType.includes('text/html') || !lowerType.startsWith('image/')) {
                return null;
            }
        }

        const existing = this.entries.get(url);
        if (existing) {
            existing.lastAccessed = ++this.accessCounter;
            return existing.objectUrl;
        }

        while (this.currentBytes + blob.size > this.maxStoreBytes && this.entries.size > 0) {
            this.evictOldest();
        }

        if (this.currentBytes + blob.size > this.maxStoreBytes) {
            return null;
        }

        const resolvedObjectUrl =
            objectUrl ?? (typeof URL !== 'undefined' && URL.createObjectURL ? URL.createObjectURL(blob) : url);
        const entry: CoverMemoryEntry = {
            url,
            objectUrl: resolvedObjectUrl,
            blob,
            bytes: blob.size,
            fetchedAt: Date.now(),
            lastAccessed: ++this.accessCounter,
        };

        this.entries.set(url, entry);
        this.currentBytes += blob.size;
        return resolvedObjectUrl;
    }

    private evictOldest(): void {
        let oldestUrl: string | undefined;
        let oldestOrder = Infinity;

        for (const [url, entry] of this.entries.entries()) {
            if (entry.lastAccessed < oldestOrder) {
                oldestOrder = entry.lastAccessed;
                oldestUrl = url;
            }
        }

        if (oldestUrl) {
            this.evict(oldestUrl);
        }
    }

    public evict(url: string): boolean {
        const entry = this.entries.get(url);
        if (!entry) {
            return false;
        }

        if (typeof URL !== 'undefined' && URL.revokeObjectURL && entry.objectUrl.startsWith('blob:')) {
            URL.revokeObjectURL(entry.objectUrl);
        }

        this.currentBytes -= entry.bytes;
        this.entries.delete(url);
        return true;
    }

    /**
     * Reconcile current library titles:
     * Evicts stored covers and cancels in-flight/queued requests for manga not in active list.
     */
    public reconcile(currentLibraryUrls: string[]): void {
        const activeSet = new Set(currentLibraryUrls);
        this.activeUrls = activeSet;
        const toEvict: string[] = [];

        for (const url of this.entries.keys()) {
            if (!activeSet.has(url)) {
                toEvict.push(url);
            }
        }

        for (const url of toEvict) {
            this.evict(url);
        }

        // Cancel in-flight requests that are no longer in library
        for (const [url, controller] of this.abortControllers.entries()) {
            if (!activeSet.has(url)) {
                controller.abort(new Error('Removed from library during fetch'));
                // The transfer finalizer owns its slot until cancellation settles.
            }
        }

        // Filter background queue
        this.backgroundQueue = this.backgroundQueue.filter((url) => activeSet.has(url));
        this.queuedSet = new Set(this.backgroundQueue);
    }

    /**
     * Clear all stored object URLs and abort pending requests on logout, account switch, or server change.
     * Uses generation guard to ensure late-resolving promises cannot store old bytes or corrupt active count.
     */
    public clear(): void {
        this.generation++;

        for (const controller of this.abortControllers.values()) {
            controller.abort(new Error('Store cleared'));
        }
        this.abortControllers.clear();

        for (const entry of this.entries.values()) {
            if (typeof URL !== 'undefined' && URL.revokeObjectURL && entry.objectUrl.startsWith('blob:')) {
                URL.revokeObjectURL(entry.objectUrl);
            }
        }

        this.entries.clear();
        this.currentBytes = 0;
        this.backgroundQueue = [];
        this.queuedSet.clear();
        this.activeUrls = undefined;
        this.activeForegroundTransfers = 0;
    }

    public onForegroundStart(): () => void {
        this.activeForegroundTransfers++;
        const generation = this.generation;
        let finished = false;
        // Preemptively abort in-flight background transfers to free socket capacity for foreground
        for (const [url, controller] of this.abortControllers) {
            // Put back in queue to retry when foreground is idle
            if (!this.queuedSet.has(url) && !this.entries.has(url)) {
                this.backgroundQueue.unshift(url);
                this.queuedSet.add(url);
            }
            controller.abort(new Error('Preempted by foreground image request'));
        }
        return () => {
            if (!finished && this.generation === generation) this.onForegroundEnd();
            finished = true;
        };
    }

    public onForegroundEnd(): void {
        this.activeForegroundTransfers = Math.max(0, this.activeForegroundTransfers - 1);
        if (this.activeForegroundTransfers === 0) {
            this.pump();
        }
    }

    public enqueue(urls: string[]): void {
        for (const url of urls) {
            if (!this.entries.has(url) && !this.queuedSet.has(url) && !this.abortControllers.has(url)) {
                this.backgroundQueue.push(url);
                this.queuedSet.add(url);
            }
        }
        this.pump();
    }

    public isPaused(): boolean {
        return this.activeForegroundTransfers > 0;
    }

    public pump(): void {
        if (this.isPaused() || !this.fetcher) {
            return;
        }

        while (
            this.abortControllers.size < this.backgroundConcurrency &&
            this.backgroundQueue.length > 0 &&
            !this.isPaused()
        ) {
            const url = this.backgroundQueue.shift()!;
            this.queuedSet.delete(url);

            if (this.entries.has(url) || (this.activeUrls && !this.activeUrls.has(url))) {
                continue;
            }

            const transferGen = this.generation;
            const controller = new AbortController();
            this.abortControllers.set(url, controller);

            // Bounded timeout so missing/hung image cannot block a slot indefinitely
            let timeoutId: ReturnType<typeof setTimeout>;
            const aborted = new Promise<never>((_, reject) => {
                controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true });
                timeoutId = setTimeout(() => controller.abort(new Error('Background fetch timed out')), this.fetchTimeoutMs);
            });
            Promise.race([Promise.resolve().then(() => this.fetcher!(url, controller.signal)), aborted])
                .then((blob) => {
                    if (this.generation === transferGen && !controller.signal.aborted &&
                        (!this.activeUrls || this.activeUrls.has(url))) this.put(url, blob);
                })
                .catch(() => {})
                .finally(() => {
                    clearTimeout(timeoutId);
                    if (this.abortControllers.get(url) === controller) this.abortControllers.delete(url);
                    if (this.generation === transferGen) this.pump();
                });
        }
    }

    public getStats(): {
        entryCount: number;
        totalBytes: number;
        maxStoreBytes: number;
        maxImageBytes: number;
        queueLength: number;
        activeBackgroundTransfers: number;
        activeForegroundTransfers: number;
        isPaused: boolean;
        generation: number;
    } {
        return {
            entryCount: this.entries.size,
            totalBytes: this.currentBytes,
            maxStoreBytes: this.maxStoreBytes,
            maxImageBytes: this.maxImageBytes,
            queueLength: this.backgroundQueue.length,
            activeBackgroundTransfers: this.abortControllers.size,
            activeForegroundTransfers: this.activeForegroundTransfers,
            isPaused: this.isPaused(),
            generation: this.generation,
        };
    }
}

export const coverMemoryStore = new CoverMemoryStore();
