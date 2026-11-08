/**
 * Focused tests for CoverMemoryStore
 * Tests memory limits, concurrency, foreground priority, reuse across unmounts,
 * version reconciliation, server cleanup, failure resilience, subpaths, and origin restrictions.
 * Executes against the canonical TypeScript module via Node 26 --experimental-strip-types.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// Node polyfill for minimal DOM Blob / URL if not in browser environment
class MockBlob {
    constructor(chunks = [], options = {}) {
        this.size = chunks.reduce(
            (acc, c) => acc + (c.byteLength || c.length || (typeof c === 'string' ? c.length : 0)),
            0,
        );
        this.type = options.type || '';
    }
}

globalThis.Blob = globalThis.Blob || MockBlob;

let objectUrlCounter = 1;
const revokedUrls = new Set();
const createdUrls = new Set();

globalThis.URL.createObjectURL = (blob) => {
    const url = `blob:http://localhost:8080/mock-${objectUrlCounter++}`;
    createdUrls.add(url);
    return url;
};

globalThis.URL.revokeObjectURL = (url) => {
    revokedUrls.add(url);
    createdUrls.delete(url);
};

// Import CANONICAL TypeScript file directly using Node 26 type stripping
import { CoverMemoryStore } from '../suwayomi/webui-memory/CoverMemoryStore.ts';

test('Origin and path security validation with subpaths', () => {
    const standardBaseUrl = 'http://localhost:8080';
    const subpathBaseUrl = 'http://localhost:8080/suwayomi/';

    // Valid manga thumbnail URLs
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl(
            'http://localhost:8080/api/v1/manga/42/thumbnail?fetchedAt=123&sourceId=1',
            standardBaseUrl,
        ),
        true,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('/api/v1/manga/42/thumbnail', standardBaseUrl),
        true,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('/manga/101/thumbnail', standardBaseUrl),
        true,
    );

    // REAL subpath URL check
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl(
            'http://localhost:8080/suwayomi/api/v1/manga/162/thumbnail?fetchedAt=123&sourceId=1',
            subpathBaseUrl,
        ),
        true,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('/suwayomi/api/v1/manga/162/thumbnail', subpathBaseUrl),
        true,
    );

    // Cross-origin attempt must be rejected
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://attacker.com/api/v1/manga/42/thumbnail', standardBaseUrl),
        false,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('https://evil.org/manga/1/thumbnail', standardBaseUrl),
        false,
    );

    // Non-thumbnail path must be rejected
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://localhost:8080/api/v1/manga/42/chapter/1/page/1', standardBaseUrl),
        false,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://localhost:8080/api/v1/user', standardBaseUrl),
        false,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('', standardBaseUrl),
        false,
    );
});

test('Reuse across unmounts without revoking shared cache', () => {
    const store = new CoverMemoryStore();
    const url = 'http://localhost:8080/api/v1/manga/1/thumbnail?fetchedAt=100';
    const blob = new MockBlob([new Uint8Array(1024)], { type: 'image/jpeg' });

    const objUrl = store.put(url, blob);
    assert.ok(objUrl);
    assert.strictEqual(store.has(url), true);

    // Simulated Component 1 mounts and consumes image
    const entry1 = store.get(url);
    assert.strictEqual(entry1.objectUrl, objUrl);

    // Component 1 unmounts: simulate card unmount cleanup (noop)
    const cleanup1 = () => {};
    cleanup1();

    // Verify object URL was NOT revoked
    assert.strictEqual(revokedUrls.has(objUrl), false);

    // Simulated Component 2 mounts: gets same object URL
    const entry2 = store.get(url);
    assert.strictEqual(entry2.objectUrl, objUrl);
});

test('Reconciliation aborts in-flight requests and evicts removed manga', async () => {
    const store = new CoverMemoryStore();
    const urlOld = 'http://localhost:8080/api/v1/manga/1/thumbnail?fetchedAt=100';
    const urlNew = 'http://localhost:8080/api/v1/manga/1/thumbnail?fetchedAt=200';
    const urlInFlightRemoved = 'http://localhost:8080/api/v1/manga/2/thumbnail?fetchedAt=100';

    const objOld = store.put(urlOld, new MockBlob([new Uint8Array(500)], { type: 'image/jpeg' }));

    let aborted = false;
    store.setFetcher(async (u, signal) => {
        return new Promise((_, reject) => {
            signal.addEventListener('abort', () => {
                aborted = true;
                reject(new Error('aborted'));
            });
        });
    });

    store.enqueue([urlInFlightRemoved]);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 1);

    // Reconcile: active library only has urlNew
    store.reconcile([urlNew]);

    // urlOld evicted and revoked
    assert.strictEqual(store.has(urlOld), false);
    assert.ok(revokedUrls.has(objOld));

    // in-flight request was aborted
    assert.strictEqual(aborted, true);

    await new Promise((r) => setTimeout(r, 20));
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
});

test('Clear on logout / server change revokes all URLs and prevents late puts', async () => {
    const store = new CoverMemoryStore();
    const url = 'http://localhost:8080/api/v1/manga/1/thumbnail';
    const objUrl = store.put(url, new MockBlob([new Uint8Array(1000)], { type: 'image/jpeg' }));

    let lateResolve;
    store.setFetcher(async (u, signal) => {
        return new Promise((resolve) => {
            lateResolve = resolve;
        });
    });

    const pendingUrl = 'http://localhost:8080/api/v1/manga/99/thumbnail';
    store.enqueue([pendingUrl]);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 1);

    // Clear triggered by logout / server switch
    store.clear();

    assert.strictEqual(store.has(url), false);
    assert.ok(revokedUrls.has(objUrl));
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
    assert.strictEqual(store.getStats().totalBytes, 0);

    // Late resolve after clear occurs
    lateResolve(new MockBlob([new Uint8Array(500)], { type: 'image/jpeg' }));

    await new Promise((r) => setTimeout(r, 20));

    // Must NOT store late data
    assert.strictEqual(store.has(pendingUrl), false);
    // Active transfers must remain 0 and not turn negative
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
});

test('Memory limits: per-image cap, HTML rejection, and 128 MiB store LRU eviction', () => {
    const store = new CoverMemoryStore({
        maxStoreBytes: 3000,
        maxImageBytes: 1500,
    });

    // Oversized single image (> 1500 bytes) must be rejected
    const oversizedBlob = new MockBlob([new Uint8Array(2000)], { type: 'image/jpeg' });
    const res = store.put('http://localhost:8080/api/v1/manga/big/thumbnail', oversizedBlob);
    assert.strictEqual(res, null);

    // HTML response rejected
    const htmlBlob = new MockBlob(['<html><body>Login Required</body></html>'], { type: 'text/html; charset=utf-8' });
    assert.strictEqual(store.put('http://localhost:8080/api/v1/manga/login/thumbnail', htmlBlob), null);

    // Non-image content rejected
    const jsonBlob = new MockBlob(['{"error": "unauthorized"}'], { type: 'application/json' });
    assert.strictEqual(store.put('http://localhost:8080/api/v1/manga/json/thumbnail', jsonBlob), null);

    // Put image 1 (1000 bytes)
    const obj1 = store.put('http://localhost:8080/api/v1/manga/1/thumbnail', new MockBlob([new Uint8Array(1000)], { type: 'image/jpeg' }));
    // Put image 2 (1000 bytes)
    const obj2 = store.put('http://localhost:8080/api/v1/manga/2/thumbnail', new MockBlob([new Uint8Array(1000)], { type: 'image/jpeg' }));
    // Put image 3 (1000 bytes) -> total = 3000
    const obj3 = store.put('http://localhost:8080/api/v1/manga/3/thumbnail', new MockBlob([new Uint8Array(1000)], { type: 'image/jpeg' }));

    assert.strictEqual(store.getStats().totalBytes, 3000);
    assert.strictEqual(store.getStats().entryCount, 3);

    // Access image 1 so image 2 becomes LRU
    store.get('http://localhost:8080/api/v1/manga/1/thumbnail');

    // Put image 4 (1000 bytes) -> must evict image 2
    const obj4 = store.put('http://localhost:8080/api/v1/manga/4/thumbnail', new MockBlob([new Uint8Array(1000)], { type: 'image/jpeg' }));

    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/2/thumbnail'), false);
    assert.ok(revokedUrls.has(obj2));
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/1/thumbnail'), true);
    assert.strictEqual(store.getStats().totalBytes, 3000);
});

test('Concurrency limit (max 2), deduplication, and failure resilience with timeout', async () => {
    const store = new CoverMemoryStore({ backgroundConcurrency: 2, fetchTimeoutMs: 50 });
    let concurrentCount = 0;
    let maxSeenConcurrent = 0;
    const completed = [];

    store.setFetcher(async (url, signal) => {
        concurrentCount++;
        maxSeenConcurrent = Math.max(maxSeenConcurrent, concurrentCount);

        if (url.includes('hang')) {
            // Hung request that will timeout
            return new Promise((_, reject) => {
                signal.addEventListener('abort', () => reject(new Error('timed out')));
            });
        }

        await new Promise((r) => setTimeout(r, 10));
        concurrentCount--;

        if (url.includes('fail')) {
            throw new Error('404 Not Found');
        }
        completed.push(url);
        return new MockBlob([new Uint8Array(200)], { type: 'image/jpeg' });
    });

    const urls = [
        'http://localhost:8080/api/v1/manga/1/thumbnail',
        'http://localhost:8080/api/v1/manga/hang/thumbnail',
        'http://localhost:8080/api/v1/manga/fail/thumbnail',
        'http://localhost:8080/api/v1/manga/2/thumbnail',
    ];

    // Enqueue with duplicates while in-flight
    store.enqueue(urls);
    store.enqueue(['http://localhost:8080/api/v1/manga/1/thumbnail', 'http://localhost:8080/api/v1/manga/2/thumbnail']);

    // Wait until queue and active transfers drain
    await new Promise((resolve) => {
        const check = () => {
            if (store.getStats().activeBackgroundTransfers === 0 && store.getStats().queueLength === 0) {
                resolve();
            } else {
                setTimeout(check, 10);
            }
        };
        check();
    });

    assert.ok(maxSeenConcurrent <= 2, `Max concurrency observed was ${maxSeenConcurrent}, expected <= 2`);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/fail/thumbnail'), false);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/hang/thumbnail'), false);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/1/thumbnail'), true);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/2/thumbnail'), true);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
});

test('Foreground priority preempts in-flight background and resumes on idle', async () => {
    const store = new CoverMemoryStore({ backgroundConcurrency: 2 });
    let bgStarted = false;
    let bgAborted = false;

    store.setFetcher(async (u, signal) => {
        bgStarted = true;
        return new Promise((resolve, reject) => {
            signal.addEventListener('abort', () => {
                bgAborted = true;
                reject(new Error('preempted'));
            });
            setTimeout(() => resolve(new MockBlob([new Uint8Array(100)], { type: 'image/jpeg' })), 100);
        });
    });

    const url = 'http://localhost:8080/api/v1/manga/bg/thumbnail';
    store.enqueue([url]);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 1);

    // Foreground request starts
    store.onForegroundStart();
    assert.strictEqual(store.isPaused(), true);
    assert.strictEqual(bgAborted, true);

    // In-flight background was preempted and requeued
    assert.strictEqual(store.getStats().queueLength, 1);

    // Foreground finishes
    store.onForegroundEnd();
    assert.strictEqual(store.isPaused(), false);

    // Wait for resumed background fetch
    await new Promise((r) => setTimeout(r, 150));
    assert.strictEqual(store.has(url), true);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
});
