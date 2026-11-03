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

test('two mutations during one active read coalesce into one trailing run and render latest state', async () => {
  let runs = 0;
  let serverState = 0;
  let renderedState = -1;

  const loop = createPolling(async (signal) => {
    runs++;
    const snap = serverState;
    await new Promise(r => setTimeout(r, 50));
    renderedState = snap;
  }, 1000);

  // Initial read started at t=0
  await new Promise(r => setTimeout(r, 10));

  // Mutation 1
  serverState = 1;
  const p1 = loop.refreshAfterMutation();

  // Mutation 2 while initial read still in flight
  await new Promise(r => setTimeout(r, 10));
  serverState = 2;
  const p2 = loop.refreshAfterMutation();

  await Promise.all([p1, p2]);

  assert.equal(renderedState, 2, 'Rendered state must be 2');
  assert.equal(runs, 2, 'Exactly 2 runs executed (initial + trailing)');
  assert.equal(loop.waiters.length, 0, 'Waiters queue cleaned up');
  loop.dispose();
});

test('third mutation during trailing read queues one further run and satisfies caller', async () => {
  let runs = 0;
  let serverState = 0;
  let renderedState = -1;

  const loop = createPolling(async (signal) => {
    runs++;
    const snap = serverState;
    await new Promise(r => setTimeout(r, 60));
    renderedState = snap;
  }, 1000);

  await new Promise(r => setTimeout(r, 10)); // run 1 in flight reading 0

  serverState = 1;
  const pM1 = loop.refreshAfterMutation(); // queues run 2

  // Wait for run 1 to finish and run 2 to start
  await new Promise(r => setTimeout(r, 70));
  assert.equal(runs, 2, 'Trailing run 2 started');

  // Mutation 3 arrives during trailing run 2
  serverState = 3;
  const pM3 = loop.refreshAfterMutation(); // queues run 3

  await pM1;
  assert.equal(renderedState, 1, 'First trailing read rendered 1');

  await pM3;
  assert.equal(renderedState, 3, 'Second trailing read rendered 3');
  assert.equal(runs, 3, 'Exactly 3 runs total');
  loop.dispose();
});

test('hidden trailing settlement settles waiters promptly and resume enables future actions', async () => {
  let runs = 0;
  let serverState = 0;
  let renderedState = -1;

  const loop = createPolling(async (signal) => {
    runs++;
    const snap = serverState;
    await new Promise(r => setTimeout(r, 50));
    renderedState = snap;
  }, 1000);

  await new Promise(r => setTimeout(r, 10));
  serverState = 1;
  const pHidden = loop.refreshAfterMutation();

  // Pause while initial read is in flight
  await new Promise(r => setTimeout(r, 10));
  loop.pause();

  await pHidden; // settles promptly on pause
  assert.equal(loop.waiters.length, 0, 'Waiters settled on pause');

  // Resume
  loop.resume();
  await new Promise(r => setTimeout(r, 120));

  // Future mutation and refresh works
  serverState = 5;
  await loop.refreshAfterMutation();
  assert.equal(renderedState, 5, 'Future mutation renders fresh state 5');
  loop.dispose();
});

test('dispose settles all waiters immediately', async () => {
  const loop = createPolling(async (signal) => {
    await new Promise(r => setTimeout(r, 100));
  }, 1000);

  await new Promise(r => setTimeout(r, 10));
  const pFresh = loop.refreshAfterMutation();

  loop.dispose();
  await pFresh;
  assert.equal(loop.waiters.length, 0, 'Waiters cleared on dispose');
});

test('max 1 active callback at any time under overlapping refreshes', async () => {
  let activeCallbacks = 0;
  let maxActiveCallbacks = 0;

  const loop = createPolling(async (signal) => {
    activeCallbacks++;
    maxActiveCallbacks = Math.max(maxActiveCallbacks, activeCallbacks);
    await new Promise(r => setTimeout(r, 30));
    activeCallbacks--;
  }, 50);

  const promises = [
    loop.refresh(),
    loop.refreshAfterMutation(),
    loop.refreshAfterMutation(),
    loop.refresh()
  ];
  await Promise.all(promises);
  loop.dispose();

  assert.equal(maxActiveCallbacks, 1, 'At most 1 callback in flight concurrently');
});

// --- NEW DETERMINISTIC UNIT REGRESSIONS ---

test('fresh read synchronous throw settles fresh waiter and avoids retry storm', async () => {
  let runs = 0;
  let syncErrors = 0;

  const loop = createPolling((signal) => {
    runs++;
    syncErrors++;
    throw new Error('Immediate sync throw on fresh read');
  }, 60000); // 60s interval: must not retry immediately!

  // Wait for initial constructor run to settle
  await new Promise(r => setTimeout(r, 10));
  const runsBefore = runs;

  // Fresh read requested
  const pFresh = loop.refreshAfterMutation();
  await pFresh; // Must resolve/settle without hanging

  assert.equal(runs - runsBefore, 1, 'Exactly one attempt executed for the fresh demand, no retry storm');
  assert.equal(loop.waiters.length, 0, 'Waiters array cleared');
  loop.dispose();
});

test('fresh read asynchronous rejection settles fresh waiter and backs off to interval', async () => {
  let runs = 0;
  let asyncRejections = 0;

  const loop = createPolling(async (signal) => {
    runs++;
    if (runs === 2) {
      asyncRejections++;
      await new Promise(r => setTimeout(r, 20));
      throw new Error('Async network failure');
    }
  }, 80); // 80ms interval

  // Wait for initial run to settle
  await new Promise(r => setTimeout(r, 10));
  assert.equal(runs, 1, 'Initial run completed');

  // Fresh read requested (will be run 2, which fails)
  const pFresh = loop.refreshAfterMutation();
  await pFresh; // Must settle promptly despite rejection

  assert.equal(asyncRejections, 1, 'Async rejection occurred');
  assert.equal(runs, 2, 'Did not immediately loop retry after failure');

  // Wait for normal scheduled interval to fire and recover
  await new Promise(r => setTimeout(r, 150));
  assert.ok(runs >= 3, 'Normal interval resumed');
  loop.dispose();
});

test('fresh read deadline abort settles fresh waiter, backs off, and newer demand triggers attempt', async () => {
  let runs = 0;
  let aborts = 0;

  const loop = createPolling(async (signal) => {
    runs++;
    if (runs === 2) {
      await new Promise((resolve) => {
        signal.addEventListener('abort', () => {
          aborts++;
          resolve();
        });
      });
    }
  }, 1000, { deadlineMs: 30 });

  // Wait for initial run
  await new Promise(r => setTimeout(r, 10));
  assert.equal(runs, 1);

  // Fresh read requested (run 2, which times out)
  const pFresh = loop.refreshAfterMutation();
  await pFresh; // Must settle when deadline aborts

  assert.equal(aborts, 1, 'Run 2 was deadline-aborted');
  assert.equal(runs, 2, 'No immediate retry storm after deadline timeout');

  // New demand arriving after the failed attempt triggers a new attempt
  const pFresh2 = loop.refreshAfterMutation();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(runs, 3, 'New demand triggered third attempt');
  await pFresh2;
  loop.dispose();
});

test('duplicate visible/pageshow events while healthy and running coalesce', async () => {
  let runs = 0;
  const loop = createPolling(async () => {
    runs++;
  }, 1000);

  await new Promise(r => setTimeout(r, 10)); // initial run completed, timer active
  assert.equal(runs, 1);

  // Dispatch duplicate resume events
  loop.resume();
  loop.resume();
  await new Promise(r => setTimeout(r, 10));

  assert.equal(runs, 1, 'Duplicate resume calls did not trigger redundant reads');
  loop.dispose();
});
