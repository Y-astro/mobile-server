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

    _abortActive() {
      if (this.activeController) {
        try {
          this.activeController.abort();
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
      this.generation++;
      this._clearTimer();
      this._abortActive();
    }

    resume() {
      if (this.isDisposed) return;
      this.isPaused = false;
      this._clearTimer();
      // Prompt refresh on becoming visible
      this.refresh();
    }

    async refresh() {
      if (this.isDisposed) return;
      // If currently in flight, coalesce and return existing promise
      if (this.inFlightPromise) {
        return this.inFlightPromise;
      }
      // If hidden or paused, do not start new network reads
      if (this.isPaused || (typeof document !== 'undefined' && document.hidden)) {
        return;
      }

      this._clearTimer();

      const runGen = ++this.generation;
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      this.activeController = controller;

      let deadlineTimer = null;
      if (controller && this.deadlineMs > 0) {
        deadlineTimer = setTimeout(() => {
          try {
            controller.abort(new Error('Polling read deadline exceeded'));
          } catch (_) {}
        }, this.deadlineMs);
      }

      const signal = controller ? controller.signal : null;

      this.inFlightPromise = (async () => {
        try {
          await this.fn(signal);
        } catch (err) {
          // If aborted due to lifecycle pause or deadline, keep failure isolated
          if (signal && signal.aborted) {
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

          // Only schedule next if still on the same generation and not paused/disposed
          if (!this.isDisposed && !this.isPaused && runGen === this.generation) {
            if (typeof document === 'undefined' || !document.hidden) {
              this._scheduleNext();
            }
          }
        }
      })();

      return this.inFlightPromise;
    }

    dispose() {
      this.isDisposed = true;
      this.isPaused = true;
      this.generation++;
      this._clearTimer();
      this._abortActive();

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
