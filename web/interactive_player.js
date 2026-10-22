/* ===================================================
   GOOGLE FILES-STYLE INTERACTIVE WEB MEDIA PLAYER
   Floating persistent background audio player
   =================================================== */

(function() {
  if (window.__interactive_player_loaded) return;
  window.__interactive_player_loaded = true;

  console.log("[Player] Initializing Google Files Web Media Player...");

  // Audio state
  let currentAudio = new Audio();
  let playlist = [];
  let currentIndex = -1;
  let isExpanded = false;

  // Supported audio extensions
  const AUDIO_EXTS = ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac', '.opus', '.wma'];

  function isAudioFile(filename) {
    if (!filename) return false;
    const lower = filename.toLowerCase();
    return AUDIO_EXTS.some(ext => lower.endsWith(ext));
  }

  function formatTime(secs) {
    if (isNaN(secs) || secs < 0) return "0:00";
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
  }

  // Create UI Container
  const playerContainer = document.createElement('div');
  playerContainer.id = 'gfiles-player-root';
  playerContainer.innerHTML = `
    <style>
      #gfiles-player-bar {
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        z-index: 999999;
        background: rgba(14, 12, 10, 0.94);
        backdrop-filter: blur(28px) saturate(180%);
        -webkit-backdrop-filter: blur(28px) saturate(180%);
        border-top: 1px solid rgba(245, 158, 11, 0.35);
        box-shadow: 0 -10px 40px rgba(0, 0, 0, 0.85), 0 0 25px rgba(245, 158, 11, 0.15);
        color: #fdfaf6;
        font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
        padding: 10px 16px;
        display: none;
        align-items: center;
        justify-content: space-between;
        gap: 14px;
        transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
      }

      .gfiles-disc {
        width: 44px;
        height: 44px;
        border-radius: 50%;
        background: linear-gradient(135deg, #1c1712, #2d241c);
        border: 2px solid #f59e0b;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #f59e0b;
        box-shadow: 0 0 15px rgba(245, 158, 11, 0.35);
        flex-shrink: 0;
        cursor: pointer;
      }
      .gfiles-disc.playing {
        animation: gfiles-spin 6s linear infinite;
      }
      @keyframes gfiles-spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
      }

      .gfiles-info {
        flex: 1;
        min-width: 0;
        cursor: pointer;
      }
      .gfiles-title {
        font-weight: 700;
        font-size: 0.88rem;
        color: #ffffff;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .gfiles-sub {
        font-size: 0.72rem;
        color: rgba(245, 158, 11, 0.85);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .gfiles-center {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        flex: 2;
        max-width: 500px;
      }
      .gfiles-controls {
        display: flex;
        align-items: center;
        gap: 12px;
      }
      .gfiles-btn {
        background: transparent;
        border: none;
        color: #fdfaf6;
        cursor: pointer;
        padding: 6px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.2s;
      }
      .gfiles-btn:hover {
        color: #fbbf24;
        background: rgba(245, 158, 11, 0.15);
      }
      .gfiles-play-btn {
        width: 38px;
        height: 38px;
        border-radius: 50%;
        background: linear-gradient(135deg, #f59e0b, #fbbf24);
        color: #0c0a09;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 4px 14px rgba(245, 158, 11, 0.5);
      }
      .gfiles-play-btn:hover {
        transform: scale(1.06);
        filter: brightness(1.1);
      }

      .gfiles-progress-wrap {
        width: 100%;
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 0.72rem;
        font-family: 'JetBrains Mono', monospace;
        color: rgba(253, 250, 246, 0.6);
      }
      .gfiles-slider {
        flex: 1;
        -webkit-appearance: none;
        height: 4px;
        border-radius: 2px;
        background: rgba(255, 255, 255, 0.15);
        outline: none;
        cursor: pointer;
      }
      .gfiles-slider::-webkit-slider-thumb {
        -webkit-appearance: none;
        width: 12px;
        height: 12px;
        border-radius: 50%;
        background: #f59e0b;
        box-shadow: 0 0 8px #f59e0b;
        cursor: pointer;
      }

      .gfiles-right {
        display: flex;
        align-items: center;
        gap: 6px;
        flex-shrink: 0;
      }

      /* Expanded Sheet */
      #gfiles-expanded-sheet {
        position: fixed;
        bottom: 74px;
        right: 16px;
        width: 340px;
        background: rgba(18, 15, 12, 0.98);
        backdrop-filter: blur(32px);
        border: 1px solid rgba(245, 158, 11, 0.3);
        border-radius: 24px;
        padding: 20px;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.9);
        z-index: 999999;
        display: none;
        flex-direction: column;
        gap: 16px;
      }
      .gfiles-album-art {
        width: 100%;
        height: 180px;
        border-radius: 18px;
        background: linear-gradient(135deg, #241c14, #120e0a);
        border: 1px solid rgba(245, 158, 11, 0.2);
        display: flex;
        align-items: center;
        justify-content: center;
        color: #f59e0b;
      }
    </style>

    <!-- Bottom Bar -->
    <div id="gfiles-player-bar">
      <div class="gfiles-disc" id="gfiles-disc-icon" onclick="toggleExpand()">
        <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
      </div>

      <div class="gfiles-info" onclick="toggleExpand()">
        <div class="gfiles-title" id="gfiles-track-name">Select a song...</div>
        <div class="gfiles-sub" id="gfiles-artist-name">Family Cloud Media</div>
      </div>

      <div class="gfiles-center">
        <div class="gfiles-controls">
          <button class="gfiles-btn" onclick="playPrev()" title="Previous Track">
            <svg width="18" height="18" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>
          </button>
          <button class="gfiles-btn" onclick="seekRelative(-10)" title="Rewind 10s">
            <svg width="18" height="18" fill="currentColor" viewBox="0 0 24 24"><path d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.16 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z"/></svg>
          </button>
          <button class="gfiles-play-btn" id="gfiles-btn-play" onclick="togglePlay()" title="Play / Pause">
            <svg id="gfiles-icon-play" width="20" height="20" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
          </button>
          <button class="gfiles-btn" onclick="seekRelative(10)" title="Forward 10s">
            <svg width="18" height="18" fill="currentColor" viewBox="0 0 24 24"><path d="M11.5 8c2.65 0 5.05.99 6.9 2.6L22 7v9h-9l3.62-3.62c-1.39-1.16-3.16-1.88-5.12-1.88-3.54 0-6.55 2.31-7.6 5.5l-2.37-.78C2.92 11.03 6.85 8 11.5 8z"/></svg>
          </button>
          <button class="gfiles-btn" onclick="playNext()" title="Next Track">
            <svg width="18" height="18" fill="currentColor" viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>
          </button>
        </div>

        <div class="gfiles-progress-wrap">
          <span id="gfiles-curr-time">0:00</span>
          <input type="range" class="gfiles-slider" id="gfiles-seek-slider" min="0" max="100" value="0" oninput="handleSeek(this.value)">
          <span id="gfiles-total-time">0:00</span>
        </div>
      </div>

      <div class="gfiles-right">
        <button class="gfiles-btn" onclick="toggleLoop()" id="gfiles-loop-btn" title="Repeat Track">
          <svg width="18" height="18" fill="currentColor" viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>
        </button>
        <button class="gfiles-btn" onclick="closePlayer()" title="Close Player">
          <svg width="18" height="18" fill="currentColor" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
        </button>
      </div>
    </div>

    <!-- Expanded Popup Card -->
    <div id="gfiles-expanded-sheet">
      <div class="gfiles-album-art">
        <svg width="64" height="64" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 14.5c-2.49 0-4.5-2.01-4.5-4.5S9.51 7.5 12 7.5s4.5 2.01 4.5 4.5-2.01 4.5-4.5 4.5zm0-5.5c-.55 0-1 .45-1 1s.45 1 1 1 1-.45 1-1-.45-1-1-1z"/></svg>
      </div>
      <div>
        <h4 id="gfiles-exp-title" style="font-weight:700;color:#fff;font-size:1.05rem;margin:0 0 4px 0;">Track</h4>
        <span id="gfiles-exp-sub" style="font-size:0.8rem;color:#f59e0b;font-weight:600;">Album Queue</span>
      </div>
      <div style="font-size:0.75rem;color:rgba(255,255,255,0.6);" id="gfiles-queue-info">Playing from current directory</div>
      <button onclick="toggleExpand()" class="gfiles-btn" style="width:100%;background:rgba(245,158,11,0.2);color:#fbbf24;border-radius:12px;padding:8px 0;font-weight:700;">Minimize Player</button>
    </div>
  `;
  document.body.appendChild(playerContainer);

  // Audio Event Listeners
  currentAudio.addEventListener('timeupdate', () => {
    if (!currentAudio.duration) return;
    const pct = (currentAudio.currentTime / currentAudio.duration) * 100;
    const slider = document.getElementById('gfiles-seek-slider');
    if (slider) slider.value = pct;
    const currSpan = document.getElementById('gfiles-curr-time');
    if (currSpan) currSpan.innerText = formatTime(currentAudio.currentTime);
  });

  currentAudio.addEventListener('loadedmetadata', () => {
    const totalSpan = document.getElementById('gfiles-total-time');
    if (totalSpan) totalSpan.innerText = formatTime(currentAudio.duration);
  });

  currentAudio.addEventListener('play', () => {
    updatePlayIcon(true);
    document.getElementById('gfiles-disc-icon')?.classList.add('playing');
  });

  currentAudio.addEventListener('pause', () => {
    updatePlayIcon(false);
    document.getElementById('gfiles-disc-icon')?.classList.remove('playing');
  });

  currentAudio.addEventListener('ended', () => {
    playNext();
  });

  function updatePlayIcon(isPlaying) {
    const btn = document.getElementById('gfiles-btn-play');
    if (!btn) return;
    if (isPlaying) {
      btn.innerHTML = '<svg width="20" height="20" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
    } else {
      btn.innerHTML = '<svg width="20" height="20" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
    }
  }

  // Play Functions
  window.playTrack = function(url, title, folder) {
    console.log("[Player] Playing track:", title, url);
    currentAudio.src = url;
    currentAudio.play().catch(e => console.warn("Autoplay blocked:", e));

    document.getElementById('gfiles-track-name').innerText = title;
    document.getElementById('gfiles-artist-name').innerText = folder || 'Audio Stream';
    document.getElementById('gfiles-exp-title').innerText = title;
    document.getElementById('gfiles-exp-sub').innerText = folder || 'Now Playing';
    document.getElementById('gfiles-player-bar').style.display = 'flex';
  };

  window.togglePlay = function() {
    if (currentAudio.paused) {
      currentAudio.play();
    } else {
      currentAudio.pause();
    }
  };

  window.handleSeek = function(val) {
    if (!currentAudio.duration) return;
    currentAudio.currentTime = (val / 100) * currentAudio.duration;
  };

  window.seekRelative = function(secs) {
    currentAudio.currentTime = Math.max(0, Math.min(currentAudio.duration || 0, currentAudio.currentTime + secs));
  };

  window.playNext = function() {
    if (playlist.length > 0 && currentIndex < playlist.length - 1) {
      currentIndex++;
      const next = playlist[currentIndex];
      playTrack(next.url, next.name, next.folder);
    }
  };

  window.playPrev = function() {
    if (playlist.length > 0 && currentIndex > 0) {
      currentIndex--;
      const prev = playlist[currentIndex];
      playTrack(prev.url, prev.name, prev.folder);
    }
  };

  window.toggleLoop = function() {
    currentAudio.loop = !currentAudio.loop;
    const btn = document.getElementById('gfiles-loop-btn');
    if (btn) {
      btn.style.color = currentAudio.loop ? '#f59e0b' : '#fdfaf6';
    }
  };

  window.toggleExpand = function() {
    isExpanded = !isExpanded;
    const sheet = document.getElementById('gfiles-expanded-sheet');
    if (sheet) sheet.style.display = isExpanded ? 'flex' : 'none';
  };

  window.closePlayer = function() {
    currentAudio.pause();
    document.getElementById('gfiles-player-bar').style.display = 'none';
    document.getElementById('gfiles-expanded-sheet').style.display = 'none';
    isExpanded = false;
  };

  // Intercept FileBrowser Audio Clicks
  function scanAndIntercept() {
    const items = document.querySelectorAll('.item[data-dir="false"], .item[aria-label]');
    items.forEach(item => {
      const label = item.getAttribute('aria-label') || item.innerText || '';
      if (isAudioFile(label)) {
        if (item.dataset.playerAttached) return;
        item.dataset.playerAttached = "true";

        // Click intercept
        item.addEventListener('click', function(e) {
          // If click was on an action or checkbox, skip
          if (e.target.closest('.action') || e.target.closest('input[type="checkbox"]')) return;

          e.preventDefault();
          e.stopPropagation();

          // Extract track link
          const link = item.querySelector('a')?.getAttribute('href') || window.location.pathname;
          // Build FileBrowser raw download URL
          // File Browser uses /api/raw/... for streaming files
          let currentPath = window.location.pathname.replace(/^\/drive\/files\/?/, "");
          let trackPath = currentPath ? (currentPath.replace(/\/$/, '') + '/' + label) : label;
          let jwt = localStorage.getItem('jwt') || '';
          let rawUrl = `/drive/api/raw/${encodeURIComponent(trackPath).replace(/%2F/g, '/')}?auth=${jwt}`;

          // Update playlist
          collectFolderPlaylist();
          currentIndex = playlist.findIndex(p => p.name === label);

          playTrack(rawUrl, label, currentPath || 'Root');
        }, true);
      }
    });
  }

  function collectFolderPlaylist() {
    playlist = [];
    const items = document.querySelectorAll('.item');
    let currentPath = window.location.pathname.replace(/^\/drive\/files\/?/, "");
    let jwt = localStorage.getItem('jwt') || '';

    items.forEach(item => {
      const label = item.getAttribute('aria-label') || item.innerText || '';
      if (isAudioFile(label)) {
        let trackPath = currentPath ? (currentPath.replace(/\/$/, '') + '/' + label) : label;
        let rawUrl = `/drive/api/raw/${encodeURIComponent(trackPath).replace(/%2F/g, '/')}?auth=${jwt}`;
        playlist.push({
          name: label,
          url: rawUrl,
          folder: currentPath || 'Root'
        });
      }
    });
  }

  // Periodic DOM check for SPA navigation
  setInterval(scanAndIntercept, 1200);

  // Keyboard controls
  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.code === 'Space') {
      e.preventDefault();
      togglePlay();
    } else if (e.code === 'ArrowRight' && e.shiftKey) {
      seekRelative(10);
    } else if (e.code === 'ArrowLeft' && e.shiftKey) {
      seekRelative(-10);
    }
  });

  console.log("[Player] Google Files Interactive Web Player successfully mounted.");
})();
