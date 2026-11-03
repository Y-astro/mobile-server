/**
 * Portal Polling Lifecycle Helper
 *
 * Provides owned, single-flight, deadline-bounded, visibility/pagehide/pageshow-aware
 * background polling. Retains state across tab switching and back/forward cache.
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
      this.generation = 0;
      this.hasPendingResume = false;
      this.pendingFreshGen = 0;
      this.trailingPromise = null;

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
        // Prompt initial execution deferred to next microtask so constructor finishes cleanly
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
      this.generation++;
      this._clearTimer();
      this._abortActive('lifecycle_pause');
    }

    resume() {
      if (this.isDisposed) return;
      this.isPaused = false;
      this._clearTimer();
      if (this.inFlightPromise) {
        // Active aborted run is still settling; remember pending resume to kick next read after it settles
        this.hasPendingResume = true;
      } else {
        this.refresh();
      }
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

      if (this.inFlightPromise) {
        if (isFresh) {
          // A mutation or fresh read requested while a pre-mutation read is in flight.
          // Enqueue at most one trailing fresh read that will run after current in-flight settles.
          const freshGen = ++this.pendingFreshGen;
          if (!this.trailingPromise) {
            this.trailingPromise = (async () => {
              try {
                await this.inFlightPromise;
              } catch (_) {}
              // After in-flight settles, if still current fresh request and not disposed/hidden, run fresh read
              if (this.isDisposed || this.isPaused || (typeof document !== 'undefined' && document.hidden)) {
                return;
              }
              if (freshGen === this.pendingFreshGen) {
                this.trailingPromise = null;
                return await this.refresh();
              }
            })();
          }
          return this.trailingPromise;
        }
        return this.inFlightPromise;
      }

      this._clearTimer();

      const runGen = ++this.generation;
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

      // Microtask deferral ensures inFlightPromise is assigned and state established
      // even if this.fn throws synchronously.
      this.inFlightPromise = (async () => {
        await Promise.resolve();
        try {
          await this.fn(signal);
        } catch (err) {
          // If aborted due to lifecycle pause, keep failure silent
          if (signal && signal.aborted && signal.reason === 'lifecycle_pause') {
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

          // Check if a resume or trailing fresh read was queued while settling
          if (!this.isDisposed && !this.isPaused && (typeof document === 'undefined' || !document.hidden)) {
            if (this.hasPendingResume) {
              this.hasPendingResume = false;
              this.refresh();
            } else if (runGen === this.generation) {
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
      this.generation++;
      this._clearTimer();
      this._abortActive('lifecycle_dispose');

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
