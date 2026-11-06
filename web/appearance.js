/**
 * =========================================================================
 * MATERIAL 3 CLOUD & PORTAL APPEARANCE CONTROLLER
 * Shared controller: runs as classic deferred script & ES side-effect module
 * Zero top-level export/import. Single initialization guard.
 * =========================================================================
 */
(function () {
  'use strict';

  if (window.__MOBILE_APPEARANCE_INITIALIZED__) return;
  window.__MOBILE_APPEARANCE_INITIALIZED__ = true;

  const STORAGE_KEY = 'mobile.appearance.v1';
  const DEFAULT_BLUR = 50; // maps linearly 0..30px -> 15px
  const DEFAULT_TRANSLUCENCY = 35; // 0% opaque, 100% clear -> alpha 0.65

  let state = {
    version: 1,
    wallpaperId: null,
    blur: DEFAULT_BLUR,
    translucency: DEFAULT_TRANSLUCENCY
  };

  let manifest = null;
  let manifestStatus = 'idle'; // 'idle' | 'loading' | 'success' | 'unauthenticated' | 'error'
  let currentLoadSeq = 0;
  let requestedWallpaperUrl = null;
  let activeTriggerEl = null;

  function validId(id) {
    return typeof id === 'string' && id.length === 64 && /^[0-9a-f]+$/.test(id);
  }

  // Safe localStorage accessor
  function loadStoredState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && parsed.version === 1) {
        if (Number.isFinite(parsed.blur) && parsed.blur >= 0 && parsed.blur <= 100) {
          state.blur = Math.round(parsed.blur);
        }
        if (Number.isFinite(parsed.translucency) && parsed.translucency >= 0 && parsed.translucency <= 100) {
          state.translucency = Math.round(parsed.translucency);
        }
        if (validId(parsed.wallpaperId)) {
          state.wallpaperId = parsed.wallpaperId;
        } else if (parsed.wallpaperId === null) {
          state.wallpaperId = null;
        }
      }
    } catch (e) {
      // Storage blocked or corrupt; keep defaults in memory
    }
  }

  function saveStoredState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        version: 1,
        wallpaperId: state.wallpaperId,
        blur: state.blur,
        translucency: state.translucency
      }));
    } catch (e) {
      // Storage blocked or quota exceeded; ignore gracefully
    }
  }

  // DOM Backdrops
  function ensureBackdropElements() {
    const parent = document.body || document.documentElement;
    if (!parent) return;
    let backdrop = document.getElementById('app-wallpaper-backdrop');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'app-wallpaper-backdrop';
      backdrop.setAttribute('aria-hidden', 'true');
      parent.prepend(backdrop);
    }
    let tint = document.getElementById('app-wallpaper-tint');
    if (!tint) {
      tint = document.createElement('div');
      tint.id = 'app-wallpaper-tint';
      tint.setAttribute('aria-hidden', 'true');
      parent.prepend(tint);
    }
  }

  // Apply appearance styles to CSS custom properties
  function applyStyles() {
    const root = document.documentElement;
    const blurPx = (state.blur * 30 / 100).toFixed(1);
    const panelAlpha = (1 - (state.translucency / 100)).toFixed(3);

    root.style.setProperty('--app-blur', `${blurPx}px`);
    root.style.setProperty('--app-translucency', `${state.translucency}%`);
    root.style.setProperty('--app-panel-alpha', panelAlpha);

    const match = manifest && manifest.wallpapers.find(w => w.id === state.wallpaperId);
    if (match) {
      preloadAndSetWallpaper(match.image_url);
      return;
    }

    // Manifest not loaded yet; preload derivative URL directly
    // Wait for the authenticated catalog before admitting a saved image ID.
    // No wallpaper selected or cleared
    if (requestedWallpaperUrl !== null) currentLoadSeq++;
    requestedWallpaperUrl = null;
    root.style.setProperty('--app-wallpaper-image', 'none');
  }

  function preloadAndSetWallpaper(url) {
    if (url === requestedWallpaperUrl) return;
    requestedWallpaperUrl = url;
    const root = document.documentElement;
    const seq = ++currentLoadSeq;
    const img = new Image();
    img.onload = function () {
      if (seq !== currentLoadSeq) return; // Stale load
      root.style.setProperty('--app-wallpaper-image', `url("${url}")`);
    };
    img.onerror = function () {
      if (seq !== currentLoadSeq) return;
      // Missing image fallback: clear wallpaper so solid M3 surface shows
      root.style.setProperty('--app-wallpaper-image', 'none');
    };
    img.src = url;
  }

  // Bounded wallpaper manifest fetch (no polling)
  async function fetchWallpapers() {
    if (manifestStatus === 'loading') return;
    manifestStatus = 'loading';
    updateGalleryUI();

    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 10000);
    try {
      const res = await fetch('/api/wallpapers', {
        headers: { 'Accept': 'application/json' },
        cache: 'no-store',
        signal: controller.signal
      });

      if (res.status === 401) {
        manifestStatus = 'unauthenticated';
        manifest = null;
        applyStyles();
        updateGalleryUI();
        return;
      }

      if (!res.ok) {
        manifestStatus = 'error';
        updateGalleryUI();
        return;
      }

      const data = await res.json();
      const ids = new Set();
      const validManifest = data && data.version === 1 && Array.isArray(data.wallpapers) &&
        data.wallpapers.every(w => {
          if (!w || !validId(w.id) || ids.has(w.id) || typeof w.name !== 'string' ||
              !Number.isInteger(w.width) || w.width <= 0 || !Number.isInteger(w.height) || w.height <= 0 ||
              w.image_url !== `/api/wallpapers/${w.id}/display.webp` ||
              w.thumbnail_url !== `/api/wallpapers/${w.id}/thumbnail.webp`) return false;
          ids.add(w.id);
          return true;
        }) && (data.default_id === null || ids.has(data.default_id));
      if (validManifest) {
        manifest = data;
        manifestStatus = 'success';

        // Reconcile selected wallpaper
        if (state.wallpaperId) {
          const exists = data.wallpapers.some(w => w.id === state.wallpaperId);
          if (!exists) {
            state.wallpaperId = data.default_id || null;
            saveStoredState();
          }
        } else if (data.default_id) {
          state.wallpaperId = data.default_id;
          saveStoredState();
        }

        applyStyles();
        updateGalleryUI();
      } else {
        manifestStatus = 'error';
        updateGalleryUI();
      }
    } catch (e) {
      manifestStatus = 'error';
      updateGalleryUI();
    } finally {
      clearTimeout(deadline);
    }
  }

  // Appearance Dialog DOM & Accessibility
  function getOrCreateDialog() {
    let dialog = document.getElementById('appearance-dialog');
    if (dialog) return dialog;

    dialog = document.createElement('dialog');
    dialog.id = 'appearance-dialog';
    dialog.setAttribute('aria-labelledby', 'appearance-dialog-title');

    dialog.innerHTML = `
      <div class="appearance-dialog-container">
        <div class="appearance-header">
          <div class="appearance-title-group">
            <h2 id="appearance-dialog-title">Appearance</h2>
            <p class="appearance-subtitle">Saved in this browser</p>
          </div>
          <button type="button" class="appearance-close-btn" id="appearance-close" aria-label="Close appearance settings">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>

        <div>
          <div class="appearance-section-title">Wallpaper</div>
          <div class="appearance-gallery-wrap">
            <div class="appearance-gallery" id="appearance-gallery" role="group" aria-label="Wallpapers">
              <div class="appearance-empty-state">Loading wallpapers…</div>
            </div>
          </div>
        </div>

        <div class="appearance-control-group">
          <div class="appearance-slider-label-row">
            <label for="appearance-blur-input">Blur (0–30px)</label>
            <span class="appearance-slider-value" id="appearance-blur-val">${state.blur}%</span>
          </div>
          <div class="appearance-slider-wrap">
            <input type="range" class="appearance-slider" id="appearance-blur-input" min="0" max="100" value="${state.blur}" aria-label="Blur percentage" />
          </div>
        </div>

        <div class="appearance-control-group">
          <div class="appearance-slider-label-row">
            <label for="appearance-translucency-input">Translucency (0–100%)</label>
            <span class="appearance-slider-value" id="appearance-translucency-val">${state.translucency}%</span>
          </div>
          <div class="appearance-slider-wrap">
            <input type="range" class="appearance-slider" id="appearance-translucency-input" min="0" max="100" value="${state.translucency}" aria-label="Translucency percentage" />
          </div>
        </div>

        <div class="appearance-actions">
          <button type="button" class="appearance-action-btn appearance-btn-reset" id="appearance-reset-btn">Reset</button>
          <button type="button" class="appearance-action-btn appearance-btn-done" id="appearance-done-btn">Done</button>
        </div>
      </div>
    `;

    document.body.appendChild(dialog);

    // Event Bindings
    const closeBtn = dialog.querySelector('#appearance-close');
    const doneBtn = dialog.querySelector('#appearance-done-btn');
    const resetBtn = dialog.querySelector('#appearance-reset-btn');
    const blurInput = dialog.querySelector('#appearance-blur-input');
    const translucencyInput = dialog.querySelector('#appearance-translucency-input');

    const handleClose = () => closeDialog();
    closeBtn.addEventListener('click', handleClose);
    doneBtn.addEventListener('click', handleClose);

    dialog.addEventListener('cancel', (e) => {
      e.preventDefault();
      closeDialog();
    });

    blurInput.addEventListener('input', (e) => {
      state.blur = parseInt(e.target.value, 10);
      document.getElementById('appearance-blur-val').textContent = `${state.blur}%`;
      saveStoredState();
      applyStyles();
    });

    translucencyInput.addEventListener('input', (e) => {
      state.translucency = parseInt(e.target.value, 10);
      document.getElementById('appearance-translucency-val').textContent = `${state.translucency}%`;
      saveStoredState();
      applyStyles();
    });

    resetBtn.addEventListener('click', () => {
      state.blur = DEFAULT_BLUR;
      state.translucency = DEFAULT_TRANSLUCENCY;
      if (manifest && manifest.default_id) {
        state.wallpaperId = manifest.default_id;
      }
      requestedWallpaperUrl = null;
      blurInput.value = state.blur;
      translucencyInput.value = state.translucency;
      document.getElementById('appearance-blur-val').textContent = `${state.blur}%`;
      document.getElementById('appearance-translucency-val').textContent = `${state.translucency}%`;
      saveStoredState();
      applyStyles();
      updateGalleryUI();
    });

    return dialog;
  }

  function updateGalleryUI() {
    const galleryEl = document.getElementById('appearance-gallery');
    if (!galleryEl) return;

    if (manifestStatus === 'loading') {
      galleryEl.innerHTML = '<div class="appearance-empty-state">Loading wallpapers…</div>';
      return;
    }

    if (manifestStatus === 'unauthenticated') {
      galleryEl.innerHTML = '<div class="appearance-notice">Sign in to customize wallpaper</div>';
      return;
    }

    if (manifestStatus === 'error') {
      galleryEl.innerHTML = `
        <div class="appearance-error">
          <span>Wallpaper library unavailable</span>
          <button type="button" class="appearance-retry-btn" id="appearance-retry-btn">Retry</button>
        </div>
      `;
      const retryBtn = galleryEl.querySelector('#appearance-retry-btn');
      if (retryBtn) retryBtn.addEventListener('click', () => fetchWallpapers());
      return;
    }

    if (!manifest || !manifest.wallpapers || manifest.wallpapers.length === 0) {
      galleryEl.innerHTML = '<div class="appearance-empty-state">No wallpapers available</div>';
      return;
    }

    // Render wallpaper buttons safely using DOM APIs
    galleryEl.innerHTML = '';
    manifest.wallpapers.forEach(wp => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'appearance-thumb-btn';
      btn.setAttribute('data-id', wp.id);
      btn.setAttribute('aria-label', wp.name || 'Wallpaper');
      const isSelected = (state.wallpaperId === wp.id);
      btn.setAttribute('aria-pressed', isSelected ? 'true' : 'false');

      const img = document.createElement('img');
      img.className = 'appearance-thumb-img';
      img.alt = wp.name || '';
      img.loading = 'lazy';
      img.src = wp.thumbnail_url || wp.image_url;

      const badge = document.createElement('span');
      badge.className = 'appearance-thumb-badge';
      badge.textContent = '✓';

      btn.appendChild(img);
      btn.appendChild(badge);

      btn.addEventListener('click', () => {
        state.wallpaperId = wp.id;
        requestedWallpaperUrl = null;
        saveStoredState();
        applyStyles();
        updateGalleryUI();
      });

      galleryEl.appendChild(btn);
    });
  }

  function openDialog(triggerEl) {
    activeTriggerEl = triggerEl || document.activeElement;
    const dialog = getOrCreateDialog();

    // Sync input values with current state
    const blurInput = dialog.querySelector('#appearance-blur-input');
    const translucencyInput = dialog.querySelector('#appearance-translucency-input');
    if (blurInput) blurInput.value = state.blur;
    if (translucencyInput) translucencyInput.value = state.translucency;
    const blurVal = dialog.querySelector('#appearance-blur-val');
    const transVal = dialog.querySelector('#appearance-translucency-val');
    if (blurVal) blurVal.textContent = `${state.blur}%`;
    if (transVal) transVal.textContent = `${state.translucency}%`;

    if (!manifest && manifestStatus === 'idle') {
      fetchWallpapers();
    } else {
      updateGalleryUI();
    }

    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
    }
  }

  function closeDialog() {
    const dialog = document.getElementById('appearance-dialog');
    if (dialog) {
      if (typeof dialog.close === 'function') {
        dialog.close();
      } else {
        dialog.removeAttribute('open');
      }
    }
    if (activeTriggerEl && typeof activeTriggerEl.focus === 'function') {
      activeTriggerEl.focus();
    }
    activeTriggerEl = null;
  }

  // Mount Appearance Button into Cloud (FileBrowser) Header
  // Mount Appearance Button into Cloud (FileBrowser) Header
  function mountCloudAppearanceButton() {
    if (document.getElementById('cloud-appearance-btn')) return;

    // FileBrowser header elements
    const header = document.querySelector('header');
    if (!header) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'cloud-appearance-btn';
    btn.className = 'action';
    btn.setAttribute('aria-label', 'Appearance');
    btn.setAttribute('title', 'Appearance');
    btn.innerHTML = `
      <i class="material-icons" style="font-size: 20px;">palette</i>
      <span style="font-size: 0.85rem; font-weight: 500;">Appearance</span>
    `;

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openDialog(btn);
    });

    // Mount before more menu or inside header
    const moreBtn = header.querySelector('#more');
    if (moreBtn && moreBtn.parentNode === header) {
      header.insertBefore(btn, moreBtn);
    } else {
      header.appendChild(btn);
    }
  }

  // Lifecycle initialization
  function init() {
    loadStoredState();
    ensureBackdropElements();
    applyStyles();

    // Hook existing portal buttons
    const portalBtn = document.getElementById('btn-appearance');
    if (portalBtn) {
      portalBtn.addEventListener('click', () => openDialog(portalBtn));
    }

    // Check if on FileBrowser page
    const isCloud = window.location.pathname.startsWith('/drive/');
    if (isCloud) mountCloudAppearanceButton();

    // Lifecycle-safe observer for SPA route changes and initial Vue mount
    const appContainer = document.getElementById('app') || document.body || document.documentElement;
    if (isCloud && appContainer) {
      const observer = new MutationObserver(() => {
        if (!document.getElementById('cloud-appearance-btn') && document.querySelector('header')) {
          mountCloudAppearanceButton();
        }
      });
      observer.observe(appContainer, { childList: true, subtree: true });
    }

    // Trigger non-blocking wallpaper fetch on authenticated pages
    if (window.location.pathname !== '/login') {
      fetchWallpapers();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose public API
  window.MobileAppearance = {
    openDialog,
    closeDialog,
    getState: () => ({ ...state }),
    fetchWallpapers
  };
})();
