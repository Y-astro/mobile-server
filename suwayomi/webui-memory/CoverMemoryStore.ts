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
}

export class CoverMemoryStore {
    // 128 MiB default maximum memory
    public static readonly DEFAULT_MAX_STORE_BYTES = 128 * 1024 * 1024;
    // 16 MiB default per-image maximum
    public static readonly DEFAULT_MAX_IMAGE_BYTES = 16 * 1024 * 1024;
    // At most 2 concurrent low-priority background transfers
    public static readonly DEFAULT_BACKGROUND_CONCURRENCY = 2;

    private readonly maxStoreBytes: number;
    private readonly maxImageBytes: number;
    private readonly backgroundConcurrency: number;

    private readonly entries = new Map<string, CoverMemoryEntry>();
    private currentBytes: number = 0;
    private accessCounter: number = 0;

    private backgroundQueue: string[] = [];
    private queuedSet = new Set<string>();
    private activeBackgroundTransfers: number = 0;
    private activeForegroundTransfers: number = 0;
    private abortControllers = new Map<string, AbortController>();

    private fetcher?: (url: string, signal: AbortSignal) => Promise<Blob>;

    constructor(options: CoverMemoryStoreOptions = {}) {
        this.maxStoreBytes = options.maxStoreBytes ?? CoverMemoryStore.DEFAULT_MAX_STORE_BYTES;
        this.maxImageBytes = options.maxImageBytes ?? CoverMemoryStore.DEFAULT_MAX_IMAGE_BYTES;
        this.backgroundConcurrency = options.backgroundConcurrency ?? CoverMemoryStore.DEFAULT_BACKGROUND_CONCURRENCY;
    }

    public setFetcher(fetcher: (url: string, signal: AbortSignal) => Promise<Blob>): void {
        this.fetcher = fetcher;
    }

    /**
     * Checks if a URL is an allowed in-library manga thumbnail for the configured server.
     * Prevents cross-origin credential leaks and chapter-page/external-domain caching.
     */
    public static isAllowedCoverUrl(url: string, configuredBaseUrl?: string): boolean {
        if (!url || typeof url !== 'string') {
            return false;
        }

        try {
            const parsed = configuredBaseUrl
                ? new URL(url, configuredBaseUrl)
                : new URL(url, typeof window !== 'undefined' ? window.location.origin : 'http://localhost');

            if (configuredBaseUrl) {
                const baseParsed = new URL(configuredBaseUrl, typeof window !== 'undefined' ? window.location.origin : 'http://localhost');
                if (parsed.origin !== baseParsed.origin) {
                    return false;
                }
            }

            const pathname = parsed.pathname;
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
        if (!blob || blob.size > this.maxImageBytes) {
            return null;
        }

        // Never cache HTML responses (e.g. auth redirect, error page)
        if (blob.type && blob.type.toLowerCase().includes('text/html')) {
            return null;
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
        let oldestTime = Infinity;

        for (const [url, entry] of this.entries.entries()) {
            if (entry.lastAccessed < oldestTime) {
                oldestTime = entry.lastAccessed;
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

    public reconcile(currentLibraryUrls: string[]): void {
        const activeSet = new Set(currentLibraryUrls);
        const toEvict: string[] = [];

        for (const url of this.entries.keys()) {
            if (!activeSet.has(url)) {
                toEvict.push(url);
            }
        }

        for (const url of toEvict) {
            this.evict(url);
        }

        this.backgroundQueue = this.backgroundQueue.filter((url) => activeSet.has(url));
        this.queuedSet = new Set(this.backgroundQueue);
    }

    public clear(): void {
        for (const controller of this.abortControllers.values()) {
            controller.abort();
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
        this.activeBackgroundTransfers = 0;
    }

    public onForegroundStart(): void {
        this.activeForegroundTransfers++;
    }

    public onForegroundEnd(): void {
        this.activeForegroundTransfers = Math.max(0, this.activeForegroundTransfers - 1);
        if (this.activeForegroundTransfers === 0) {
            this.pump();
        }
    }

    public enqueue(urls: string[]): void {
        for (const url of urls) {
            if (!this.entries.has(url) && !this.queuedSet.has(url)) {
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
            this.activeBackgroundTransfers < this.backgroundConcurrency &&
            this.backgroundQueue.length > 0 &&
            !this.isPaused()
        ) {
            const url = this.backgroundQueue.shift()!;
            this.queuedSet.delete(url);

            if (this.entries.has(url)) {
                continue;
            }

            this.activeBackgroundTransfers++;
            const controller = new AbortController();
            this.abortControllers.set(url, controller);

            this.fetcher(url, controller.signal)
                .then((blob) => {
                    this.abortControllers.delete(url);
                    this.put(url, blob);
                })
                .catch(() => {
                    this.abortControllers.delete(url);
                })
                .finally(() => {
                    this.activeBackgroundTransfers--;
                    this.pump();
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
    } {
        return {
            entryCount: this.entries.size,
            totalBytes: this.currentBytes,
            maxStoreBytes: this.maxStoreBytes,
            maxImageBytes: this.maxImageBytes,
            queueLength: this.backgroundQueue.length,
            activeBackgroundTransfers: this.activeBackgroundTransfers,
            activeForegroundTransfers: this.activeForegroundTransfers,
            isPaused: this.isPaused(),
        };
    }
}

export const coverMemoryStore = new CoverMemoryStore();
