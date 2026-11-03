/**
 * Portal Polling Lifecycle Helper
 *
 * Invariant:
 * - At most one background read executes at any given time.
 * - Ordinary refresh coalesces with active in-flight work.
 * - A fresh/mutation refresh requires completion of a read started at or after the request time.
 * - Callers awaiting a fresh refresh receive a Promise that resolves only once a read satisfying
 *   their demand revision has completed (or immediately if paused/disposed).
 * - Mutations occurring during a trailing read naturally trigger another trailing read without
 *   unbounded queue accumulation.
 * - Pausing, hiding, or disposing promptly settles waiting caller promises and aborts background GETs,
 *   preserving pending-fresh intent to run on resume without starting hidden reads.
 */
(() => {
  const DEFAULT_DEADLINE_MS = 10000;

  class PollingLoop {
    constructor(fn, intervalMs, options = {}) {
      if (typeof fn !== 'function') throw new TypeError('Polling action must be a function');
      this.fn = fn;
      this.intervalMs = typeof intervalMs === 'number' && intervalMs > 0 ? intervalMs : 3000;
      this.deadlineMs = typeof options.deadlineMs === 'number' && options.deadlineMs > 0 ? options.deadlineMs : DEFAULT_DEADLINE_MS;
      this.name = options.name || 'unnamed';

      this.timerId = null;
      this.activeController = null;
      this.inFlightPromise = null;
      this.isDisposed = false;
      this.isPaused = false;
      this.hasPendingResume = false;

      // Monotonic demand and completion counters for fresh reads
      this.demandRev = 0;
      this.servedRev = 0;
      this.currentRunStartRev = 0;
      this.waiters = [];

      this._onVisibilityChange = this._onVisibilityChange.bind(this);
      this._onPageHide = this._onPageHide.bind(this);
      this._onPageShow = this._onPageShow.bind(this);

      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', this._onVisibilityChange);
      }
      if (typeof window !== 'undefined') {
        window.addEventListener('pagehide', this._onPageHide);
        window.addEventListener('pageshow', this._onPageShow);
      }

      // If document is initially hidden, start in paused state without starting timers or reads
      if (typeof document !== 'undefined' && document.hidden) {
        this.isPaused = true;
      } else {
        // Prompt initial execution
        this.refresh();
      }
    }

    _clearTimer() {
      if (this.timerId !== null) {
        clearTimeout(this.timerId);
        this.timerId = null;
      }
    }

    _abortActive(reason = 'lifecycle_pause') {
      if (this.activeController) {
        try {
          this.activeController.abort(reason);
        } catch (_) {}
        this.activeController = null;
      }
    }

    _scheduleNext() {
      this._clearTimer();
      if (this.isDisposed || this.isPaused) return;
      if (typeof document !== 'undefined' && document.hidden) return;

      this.timerId = setTimeout(() => {
        this.timerId = null;
        this.refresh();
      }, this.intervalMs);
    }

    _onVisibilityChange() {
      if (this.isDisposed) return;
      if (document.hidden) {
        this.pause();
      } else {
        this.resume();
      }
    }

    _onPageHide() {
      if (this.isDisposed) return;
      this.pause();
    }

    _onPageShow() {
      if (this.isDisposed) return;
      this.resume();
    }

    pause() {
      this.isPaused = true;
      this.hasPendingResume = false;
      this._clearTimer();
      this._abortActive('lifecycle_pause');
      // Settle any waiting callers promptly so action buttons do not hang while paused/hidden
      this._settleWaiters();
    }

    resume() {
      if (this.isDisposed) return;
      if (!this.isPaused && !this.inFlightPromise && this.timerId !== null) {
        // Duplicate visibility/pageshow event while already running normally: coalesce
        return;
      }
      this.isPaused = false;
      this._clearTimer();
      if (this.inFlightPromise) {
        // Active aborted run is still settling; remember pending resume to kick next read after it settles
        this.hasPendingResume = true;
      } else {
        this.refresh();
      }
    }

    _settleWaiters() {
      const remaining = [];
      for (const w of this.waiters) {
        if (this.isDisposed || this.isPaused || this.servedRev >= w.targetRev) {
          w.resolve();
        } else {
          remaining.push(w);
        }
      }
      this.waiters = remaining;
    }

    async refresh(options = {}) {
      if (this.isDisposed) return;

      const isFresh = Boolean(options && (options.fresh || options.trailing));

      // If hidden or paused, do not start new network reads
      if (this.isPaused || (typeof document !== 'undefined' && document.hidden)) {
        if (isFresh) {
          this.hasPendingResume = true;
        }
        return;
      }

      if (isFresh) {
        const targetRev = ++this.demandRev;
        const waiterPromise = new Promise(resolve => {
          this.waiters.push({ targetRev, resolve });
        });

        if (!this.inFlightPromise) {
          this._executeRead();
        }
        return waiterPromise;
      }

      // Ordinary non-fresh refresh coalesces with active in-flight read
      if (this.inFlightPromise) {
        return this.inFlightPromise;
      }

      return this._executeRead();
    }

    async _executeRead() {
      if (this.isDisposed) return;
      if (this.isPaused || (typeof document !== 'undefined' && document.hidden)) {
        return;
      }

      this._clearTimer();

      // Record demand revision active at the start of this read
      const runStartRev = this.demandRev;
      this.currentRunStartRev = runStartRev;

      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      this.activeController = controller;

      let deadlineTimer = null;
      if (controller && this.deadlineMs > 0) {
        deadlineTimer = setTimeout(() => {
          try {
            controller.abort('deadline_timeout');
          } catch (_) {}
        }, this.deadlineMs);
      }

      const signal = controller ? controller.signal : null;

      // Microtask deferral guarantees inFlightPromise assignment before callback evaluates
      this.inFlightPromise = (async () => {
        await Promise.resolve();
        try {
          await this.fn(signal);
          // If this run completed without being aborted, mark servedRev up to runStartRev
          if (!signal || !signal.aborted) {
            this.servedRev = Math.max(this.servedRev, runStartRev);
          }
        } catch (err) {
          // Lifecycle cancellations stay silent
          if (signal && signal.aborted && (signal.reason === 'lifecycle_pause' || signal.reason === 'lifecycle_dispose')) {
            return;
          }
        } finally {
          if (deadlineTimer !== null) {
            clearTimeout(deadlineTimer);
            deadlineTimer = null;
          }
          if (this.activeController === controller) {
            this.activeController = null;
          }
          this.inFlightPromise = null;

          // Settle any waiters whose target revision has now been served
          this._settleWaiters();

          // Check for queued resume or remaining fresh demand that arrived during execution
          if (!this.isDisposed && !this.isPaused && (typeof document === 'undefined' || !document.hidden)) {
            if (this.hasPendingResume) {
              this.hasPendingResume = false;
              this.refresh();
            } else if (this.demandRev > this.servedRev) {
              // Further fresh demand arrived during execution: run next trailing read
              this._executeRead();
            } else {
              this._scheduleNext();
            }
          }
        }
      })();

      return this.inFlightPromise;
    }

    refreshAfterMutation() {
      return this.refresh({ fresh: true });
    }

    dispose() {
      this.isDisposed = true;
      this.isPaused = true;
      this.hasPendingResume = false;
      this._clearTimer();
      this._abortActive('lifecycle_dispose');
      this._settleWaiters();

      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', this._onVisibilityChange);
      }
      if (typeof window !== 'undefined') {
        window.removeEventListener('pagehide', this._onPageHide);
        window.removeEventListener('pageshow', this._onPageShow);
      }
    }
  }

  function createPolling(fn, intervalMs, options) {
    return new PollingLoop(fn, intervalMs, options);
  }

  const PortalPolling = {
    PollingLoop,
    createPolling,
    DEFAULT_DEADLINE_MS
  };

  if (typeof window !== 'undefined') {
    window.PortalPolling = PortalPolling;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = PortalPolling;
  }
})();
