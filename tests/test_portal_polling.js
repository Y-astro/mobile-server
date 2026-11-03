const test = require('node:test');
const assert = require('node:assert/strict');
const { createPolling, PollingLoop } = require('../web/portal-polling.js');

test('synchronous callback error does not lock inFlightPromise permanently and allows reschedule', async () => {
  let runs = 0;
  const loop = createPolling((signal) => {
    runs++;
    if (runs === 1) {
      throw new Error('Synchronous throw');
    }
  }, 50);

  // Wait past 2 intervals
  await new Promise(r => setTimeout(r, 160));
  assert.ok(runs >= 2, `Expected at least 2 runs, got ${runs}`);
  loop.dispose();
});

test('resume during abort settlement remembers pending kick and schedules subsequent reads', async () => {
  let abortSettled = false;
  let runs = 0;

  const loop = createPolling(async (signal) => {
    runs++;
    if (runs === 1) {
      await new Promise(resolve => {
        signal.addEventListener('abort', () => {
          setTimeout(() => {
            abortSettled = true;
            resolve();
          }, 40);
        });
      });
    }
  }, 50);

  // Wait for first read to start
  await new Promise(r => setTimeout(r, 10));
  assert.equal(runs, 1);

  // Pause and immediately resume while abort is settling
  loop.pause();
  loop.resume();

  // Wait for settlement and subsequent scheduled reads
  await new Promise(r => setTimeout(r, 200));
  assert.ok(abortSettled, 'First read was aborted and settled');
  assert.ok(runs >= 2, `Expected at least 2 runs after resume, got ${runs}`);
  loop.dispose();
});

test('mutation freshness: refreshAfterMutation queues fresh trailing read when read in-flight', async () => {
  let serverState = 0;
  let renderedState = null;

  const loop = createPolling(async (signal) => {
    // Simulate latency reading serverState
    await new Promise(r => setTimeout(r, 40));
    renderedState = serverState;
  }, 1000);

  // First background read is now in flight
  await new Promise(r => setTimeout(r, 10));
  assert.equal(renderedState, null, 'Read still in flight');

  // Mutation commits on server
  serverState = 1;

  // Post-mutation refresh requested
  await loop.refreshAfterMutation();

  assert.equal(renderedState, 1, 'Post-mutation refresh must reflect fresh server state');
  loop.dispose();
});

test('manual concurrent refresh calls coalesce without duplicate runs', async () => {
  let runs = 0;
  const loop = createPolling(async () => {
    runs++;
    await new Promise(r => setTimeout(r, 50));
  }, 500);

  const [p1, p2, p3] = [loop.refresh(), loop.refresh(), loop.refresh()];
  await Promise.all([p1, p2, p3]);

  assert.equal(runs, 1, 'Concurrent refresh() calls coalesce into 1 run');
  loop.dispose();
});
