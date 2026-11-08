/**
 * Focused tests for CoverMemoryStore
 * Tests memory limits, concurrency, foreground priority, reuse across unmounts,
 * version reconciliation, server cleanup, failure resilience, and origin restrictions.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// Node polyfill for minimal DOM Blob / URL if not in browser environment
class MockBlob {
    constructor(chunks = [], options = {}) {
        this.size = chunks.reduce((acc, c) => acc + (c.byteLength || c.length || (typeof c === 'string' ? c.length : 0)), 0);
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

// Import CoverMemoryStore implementation
// We test against the maintained pure class
import { CoverMemoryStore } from '../suwayomi/webui-memory/CoverMemoryStore.mjs';

test('Origin and path security validation', () => {
    const baseUrl = 'http://localhost:8080';

    // Valid manga thumbnail URLs
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://localhost:8080/api/v1/manga/42/thumbnail?fetchedAt=123&sourceId=1', baseUrl),
        true,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('/api/v1/manga/42/thumbnail', baseUrl),
        true,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('/manga/101/thumbnail', baseUrl),
        true,
    );

    // Cross-origin attempt must be rejected
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://attacker.com/api/v1/manga/42/thumbnail', baseUrl),
        false,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('https://evil.org/manga/1/thumbnail', baseUrl),
        false,
    );

    // Non-thumbnail path must be rejected
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://localhost:8080/api/v1/manga/42/chapter/1/page/1', baseUrl),
        false,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('http://localhost:8080/api/v1/user', baseUrl),
        false,
    );
    assert.strictEqual(
        CoverMemoryStore.isAllowedCoverUrl('', baseUrl),
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

test('Reconciliation on version change and library removal', () => {
    const store = new CoverMemoryStore();
    const urlOld = 'http://localhost:8080/api/v1/manga/1/thumbnail?fetchedAt=100';
    const urlNew = 'http://localhost:8080/api/v1/manga/1/thumbnail?fetchedAt=200';
    const urlRemoved = 'http://localhost:8080/api/v1/manga/2/thumbnail?fetchedAt=100';

    const objOld = store.put(urlOld, new MockBlob([new Uint8Array(500)]));
    const objRemoved = store.put(urlRemoved, new MockBlob([new Uint8Array(500)]));

    // Active library now has manga 1 with new URL, and manga 2 is removed
    store.reconcile([urlNew]);

    // urlOld and urlRemoved should be evicted and revoked
    assert.strictEqual(store.has(urlOld), false);
    assert.strictEqual(store.has(urlRemoved), false);
    assert.ok(revokedUrls.has(objOld));
    assert.ok(revokedUrls.has(objRemoved));
});

test('Clear on logout / server change revokes all URLs and aborts pending', async () => {
    const store = new CoverMemoryStore();
    const url = 'http://localhost:8080/api/v1/manga/1/thumbnail';
    const objUrl = store.put(url, new MockBlob([new Uint8Array(1000)]));

    let aborted = false;
    store.setFetcher(async (u, signal) => {
        return new Promise((_, reject) => {
            signal.addEventListener('abort', () => {
                aborted = true;
                reject(new Error('aborted'));
            });
        });
    });

    store.enqueue(['http://localhost:8080/api/v1/manga/99/thumbnail']);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 1);

    store.clear();

    assert.strictEqual(store.has(url), false);
    assert.ok(revokedUrls.has(objUrl));
    assert.strictEqual(aborted, true);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
    assert.strictEqual(store.getStats().totalBytes, 0);
});

test('Memory limits: per-image cap and 128 MiB store LRU eviction', () => {
    const store = new CoverMemoryStore({
        maxStoreBytes: 3000,
        maxImageBytes: 1500,
    });

    // Oversized single image (> 1500 bytes) must be rejected
    const oversizedBlob = new MockBlob([new Uint8Array(2000)], { type: 'image/jpeg' });
    const res = store.put('http://localhost:8080/api/v1/manga/big/thumbnail', oversizedBlob);
    assert.strictEqual(res, null);

    // Put image 1 (1000 bytes)
    const obj1 = store.put('http://localhost:8080/api/v1/manga/1/thumbnail', new MockBlob([new Uint8Array(1000)]));
    // Put image 2 (1000 bytes)
    const obj2 = store.put('http://localhost:8080/api/v1/manga/2/thumbnail', new MockBlob([new Uint8Array(1000)]));
    // Put image 3 (1000 bytes) -> total = 3000
    const obj3 = store.put('http://localhost:8080/api/v1/manga/3/thumbnail', new MockBlob([new Uint8Array(1000)]));

    assert.strictEqual(store.getStats().totalBytes, 3000);
    assert.strictEqual(store.getStats().entryCount, 3);

    // Access image 1 so image 2 becomes least recently used
    store.get('http://localhost:8080/api/v1/manga/1/thumbnail');

    // Put image 4 (1000 bytes) -> must evict image 2
    const obj4 = store.put('http://localhost:8080/api/v1/manga/4/thumbnail', new MockBlob([new Uint8Array(1000)]));

    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/2/thumbnail'), false);
    assert.ok(revokedUrls.has(obj2));
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/1/thumbnail'), true);
    assert.strictEqual(store.getStats().totalBytes, 3000);
});

test('HTML response rejection prevents caching login/redirect errors', () => {
    const store = new CoverMemoryStore();
    const htmlBlob = new MockBlob(['<html><body>Login Required</body></html>'], { type: 'text/html; charset=utf-8' });

    const result = store.put('http://localhost:8080/api/v1/manga/login/thumbnail', htmlBlob);
    assert.strictEqual(result, null);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/login/thumbnail'), false);
});

test('Concurrency limit (max 2) and failure resilience', async () => {
    const store = new CoverMemoryStore({ backgroundConcurrency: 2 });
    let concurrentCount = 0;
    let maxSeenConcurrent = 0;
    const completed = [];

    store.setFetcher(async (url) => {
        concurrentCount++;
        maxSeenConcurrent = Math.max(maxSeenConcurrent, concurrentCount);
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
        'http://localhost:8080/api/v1/manga/fail/thumbnail',
        'http://localhost:8080/api/v1/manga/2/thumbnail',
        'http://localhost:8080/api/v1/manga/3/thumbnail',
    ];

    store.enqueue(urls);

    // Wait until finished
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
    assert.strictEqual(completed.length, 3);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/fail/thumbnail'), false);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/1/thumbnail'), true);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/2/thumbnail'), true);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/3/thumbnail'), true);
});

test('Foreground priority pauses background queue and resumes on idle', async () => {
    const store = new CoverMemoryStore({ backgroundConcurrency: 2 });
    let backgroundStarted = false;

    store.setFetcher(async () => {
        backgroundStarted = true;
        return new MockBlob([new Uint8Array(100)], { type: 'image/jpeg' });
    });

    // Foreground active
    store.onForegroundStart();
    assert.strictEqual(store.isPaused(), true);

    store.enqueue(['http://localhost:8080/api/v1/manga/bg/thumbnail']);
    assert.strictEqual(store.getStats().activeBackgroundTransfers, 0);
    assert.strictEqual(backgroundStarted, false);

    // Foreground ends
    store.onForegroundEnd();
    assert.strictEqual(store.isPaused(), false);

    // Wait for pump
    await new Promise((r) => setTimeout(r, 20));
    assert.strictEqual(backgroundStarted, true);
    assert.strictEqual(store.has('http://localhost:8080/api/v1/manga/bg/thumbnail'), true);
});
