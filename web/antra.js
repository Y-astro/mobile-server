/**
 * =========================================================================
 * ANTRA HI-RES MUSIC DOWNLOADER
 * Client Controller & Lifecycle Management
 * Best-available native FLAC enforcement, visibility-aware polling, zero XSS,
 * multi-destination routing (private vs family).
 * =========================================================================
 */
(() => {
  'use strict';

  // DOM Elements
  const authAlert = document.getElementById('auth-alert');
  const authAlertTitle = document.getElementById('auth-alert-title');
  const authAlertMessage = document.getElementById('auth-alert-message');
  const helperAlert = document.getElementById('helper-alert');
  const antraMain = document.getElementById('antra-main');
  const antraForm = document.getElementById('antra-form');
  const urlInput = document.getElementById('antra-url-input');
  const destinationSelect = document.getElementById('antra-destination-select');
  const submitBtn = document.getElementById('antra-submit-btn');
  const submitBtnText = document.getElementById('submit-btn-text');
  const formStatus = document.getElementById('form-status');
  const workerStateBadge = document.getElementById('worker-state-badge');
  const workerMessageVal = document.getElementById('worker-message-val');
  const outputDirPrivate = document.getElementById('output-dir-private');
  const outputDirFamily = document.getElementById('output-dir-family');
  const jobsList = document.getElementById('jobs-list');
  const jobsCountBadge = document.getElementById('jobs-count-badge');
  const jobsEmpty = document.getElementById('jobs-empty');
  const btnRefresh = document.getElementById('btn-refresh');
  const tracksSection = document.getElementById('tracks-section');
  const tracksJobTitle = document.getElementById('tracks-job-title');
  const tracksJobStatusBadge = document.getElementById('tracks-job-status-badge');
  const tracksJobDestBadge = document.getElementById('tracks-job-dest-badge');
  const tracksSummary = document.getElementById('tracks-summary');
  const tracksTbody = document.getElementById('tracks-tbody');
  const btnCloseTracks = document.getElementById('btn-close-tracks');

  // Application State
  let activeJobId = null;
  let isSubmitting = false;
  let isAccessDenied = false;
  let isPageHidden = false;
  let manualFetchInFlight = false;
  let pollingLoop = null;
  let cachedJobs = [];
  let activeTrackController = null;
  let activeMainFetchController = null;
  let pendingDuplicateUrl = null;
  let pendingDuplicateDest = null;

  // Helper: Format status badge class
  function getStatusClass(status) {
    const s = String(status || '').toLowerCase();
    switch (s) {
      case 'queued': return 'badge-status-queued';
      case 'resolving':
      case 'downloading': return 'badge-status-downloading';
      case 'completed': return 'badge-status-completed';
      case 'partial': return 'badge-status-partial';
      case 'failed': return 'badge-status-failed';
      case 'cancelled': return 'badge-status-cancelled';
      case 'blocked':
      case 'uncertain': return 'badge-status-blocked';
      default: return 'badge-status-queued';
    }
  }

  // Helper: Format track quality label
  function createQualityBadge(track) {
    const span = document.createElement('span');
    const bitDepth = track.bit_depth;
    const sampleRate = track.sample_rate;
    const rateKhz = sampleRate ? (sampleRate / 1000).toFixed(1).replace(/\.0$/, '') + ' kHz' : '';

    if (bitDepth === 24) {
      span.className = 'quality-badge-24';
      span.textContent = rateKhz ? `24-bit / ${rateKhz}` : '24-bit FLAC';
    } else if (bitDepth === 16) {
      span.className = 'quality-badge-16';
      span.textContent = rateKhz ? `16-bit / ${rateKhz}` : '16-bit FLAC';
    } else if (bitDepth) {
      span.className = 'quality-badge-16';
      span.textContent = rateKhz ? `${bitDepth}-bit / ${rateKhz}` : `${bitDepth}-bit FLAC`;
    } else if (track.status === 'skipped') {
      span.className = 'quality-badge-skipped';
      span.textContent = 'Skipped';
    } else if (track.status === 'failed') {
      span.className = 'text-xs text-muted';
      span.textContent = 'Failed';
    } else {
      span.className = 'text-xs text-muted';
      span.textContent = 'Pending stream';
    }
    return span;
  }

  // Helper: Format destination badge
  function createDestinationBadge(destination) {
    const span = document.createElement('span');
    const isFamily = destination === 'family';
    span.className = `badge-destination ${isFamily ? 'badge-destination-family' : ''}`;
    span.textContent = isFamily ? 'Family music' : 'My music (private)';
    return span;
  }

  // Handle 401 / 403 access refusal
  function handleAccessRefusal(status, message) {
    isAccessDenied = true;

    // Dispose background loop
    if (pollingLoop && typeof pollingLoop.dispose === 'function') {
      pollingLoop.dispose();
      pollingLoop = null;
    }

    // Abort active detail or manual requests
    if (activeTrackController) {
      activeTrackController.abort();
      activeTrackController = null;
    }
    if (activeMainFetchController) {
      activeMainFetchController.abort();
      activeMainFetchController = null;
    }

    // Purge all previous cached content and DOM elements
    cachedJobs = [];
    activeJobId = null;
    if (jobsList) jobsList.innerHTML = '';
    if (tracksTbody) tracksTbody.innerHTML = '';
    if (tracksSection) tracksSection.classList.add('hidden');
    if (tracksJobTitle) tracksJobTitle.textContent = '';
    if (tracksSummary) tracksSummary.textContent = '';
    if (jobsCountBadge) jobsCountBadge.textContent = '0 jobs';
    if (jobsEmpty) jobsEmpty.classList.add('hidden');

    if (authAlert) {
      authAlert.classList.remove('hidden');
      if (status === 401) {
        authAlertTitle.textContent = 'Authentication Required';
        authAlertMessage.textContent = message || 'Please sign in to access Antra music downloads.';
      } else {
        authAlertTitle.textContent = 'Access Denied (403)';
        authAlertMessage.textContent = message || 'Administrator privileges required. Family accounts cannot access Antra.';
      }
    }

    // Disable all user controls and inputs
    if (urlInput) urlInput.disabled = true;
    if (destinationSelect) destinationSelect.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    if (btnRefresh) btnRefresh.disabled = true;
  }

  // Fetch Jobs and Worker Status (Main Polling Action)
  async function fetchJobs(signal) {
    if (isAccessDenied || isPageHidden || manualFetchInFlight) return;
    manualFetchInFlight = true;

    const controller = new AbortController();
    activeMainFetchController = controller;
    const fetchSignal = AbortSignal.any([controller.signal, AbortSignal.timeout(10000), ...(signal ? [signal] : [])]);

    try {
      const res = await fetch('/api/antra/jobs', {
        headers: { 'Accept': 'application/json' },
        signal: fetchSignal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      if (!res.ok) {
        throw new Error(`Server returned HTTP ${res.status}`);
      }

      const data = await res.json();

      // Guard late or disposed state
      if (isAccessDenied || isPageHidden || fetchSignal.aborted) return;

      renderWorker(data.worker, data.settings);
      renderJobs(data.jobs || []);

      // If a track details view is currently open, refresh it as well
      if (activeJobId) {
        await fetchTrackDetails(activeJobId, signal, false);
      }
    } catch (err) {
      if (err.name === 'AbortError' || isPageHidden || isAccessDenied) return;
      if (workerStateBadge && !isAccessDenied) {
        workerStateBadge.textContent = 'Offline';
        workerStateBadge.className = 'm3-badge badge-neutral';
      }
      if (workerMessageVal && !isAccessDenied) {
        workerMessageVal.textContent = 'Unable to reach Antra service. Retrying…';
      }
    } finally {
      manualFetchInFlight = false;
      if (activeMainFetchController === controller) {
        activeMainFetchController = null;
      }
    }
  }

  // Render Worker and Output directory details
  function renderWorker(worker, settings) {
    if (!worker) return;
    if (workerStateBadge) {
      workerStateBadge.textContent = worker.state || 'Idle';
      if (worker.state === 'running' || worker.state === 'active') {
        workerStateBadge.className = 'm3-badge badge-teal';
      } else {
        workerStateBadge.className = 'm3-badge badge-neutral';
      }
    }
    if (workerMessageVal) {
      workerMessageVal.textContent = worker.message || 'Worker active';
    }
    if (settings && settings.output_directories) {
      if (outputDirPrivate && settings.output_directories.private) {
        outputDirPrivate.textContent = settings.output_directories.private;
      }
      if (outputDirFamily && settings.output_directories.family) {
        outputDirFamily.textContent = settings.output_directories.family;
      }
    }
  }

  // Render Jobs List
  function renderJobs(jobs) {
    if (isAccessDenied) return;
    cachedJobs = jobs;

    if (jobsCountBadge) {
      jobsCountBadge.textContent = `${jobs.length} ${jobs.length === 1 ? 'job' : 'jobs'}`;
    }

    if (!jobs || jobs.length === 0) {
      if (jobsEmpty) jobsEmpty.classList.remove('hidden');
      if (jobsList) jobsList.innerHTML = '';
      return;
    }

    if (jobsEmpty) jobsEmpty.classList.add('hidden');
    if (!jobsList) return;

    // Build fragment safely with textContent for all user strings
    const frag = document.createDocumentFragment();

    jobs.forEach(job => {
      const card = document.createElement('div');
      card.className = 'm3-card golden-glass-card p-4 rounded-xl space-y-3 job-card';
      card.setAttribute('data-job-id', job.id);

      // Top Header: Title, URL, Badges
      const topRow = document.createElement('div');
      topRow.className = 'flex flex-col sm:flex-row sm:items-center justify-between gap-2';

      const titleCol = document.createElement('div');
      titleCol.className = 'flex-1 min-w-0';

      const titleEl = document.createElement('h3');
      titleEl.className = 'text-sm font-semibold text-white tracking-tight truncate';
      titleEl.textContent = job.title || job.url || 'Untitled Download';

      const urlEl = document.createElement('p');
      urlEl.className = 'text-xs text-muted truncate mono';
      urlEl.textContent = job.url || '';

      titleCol.appendChild(titleEl);
      titleCol.appendChild(urlEl);

      const badgesCol = document.createElement('div');
      badgesCol.className = 'flex items-center gap-2 flex-wrap';

      // Render immutable destination badge from server
      const destBadge = createDestinationBadge(job.destination);
      badgesCol.appendChild(destBadge);

      const statusBadge = document.createElement('span');
      statusBadge.className = `badge-status ${getStatusClass(job.status)}`;
      statusBadge.textContent = job.status || 'queued';
      badgesCol.appendChild(statusBadge);

      topRow.appendChild(titleCol);
      topRow.appendChild(badgesCol);
      card.appendChild(topRow);

      // Progress Bar
      const total = typeof job.total === 'number' ? job.total : 0;
      const completed = typeof job.completed === 'number' ? job.completed : 0;
      const skipped = typeof job.skipped === 'number' ? job.skipped : 0;
      const failed = typeof job.failed === 'number' ? job.failed : 0;
      const processed = completed + skipped;
      const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : (job.status === 'completed' ? 100 : 0);

      const progTrack = document.createElement('div');
      progTrack.className = 'progress-track';
      progTrack.setAttribute('role', 'progressbar');
      progTrack.setAttribute('aria-valuenow', String(pct));
      progTrack.setAttribute('aria-valuemin', '0');
      progTrack.setAttribute('aria-valuemax', '100');
      progTrack.setAttribute('aria-label', `Download progress for ${job.title || job.url}`);

      const progFill = document.createElement('div');
      progFill.className = `progress-fill ${job.status === 'failed' ? 'fill-failed' : ''}`;
      progFill.style.width = `${pct}%`;
      progTrack.appendChild(progFill);
      card.appendChild(progTrack);

      // Metrics & Actions Row
      const metaRow = document.createElement('div');
      metaRow.className = 'flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs text-soft';

      const metricsDiv = document.createElement('div');
      metricsDiv.className = 'flex flex-wrap items-center gap-3';

      const compSpan = document.createElement('span');
      compSpan.innerHTML = `Completed: <strong class="text-white">${completed}</strong> / ${total}`;

      const skipSpan = document.createElement('span');
      skipSpan.className = 'text-muted';
      skipSpan.innerHTML = `Skipped (&lt;24b): <strong class="text-white">${skipped}</strong>`;

      metricsDiv.appendChild(compSpan);
      metricsDiv.appendChild(skipSpan);

      if (failed > 0) {
        const failSpan = document.createElement('span');
        failSpan.className = 'text-red-300';
        failSpan.innerHTML = `Failed: <strong>${failed}</strong>`;
        metricsDiv.appendChild(failSpan);
      }

      metaRow.appendChild(metricsDiv);

      // Action Buttons
      const actionsDiv = document.createElement('div');
      actionsDiv.className = 'flex items-center gap-2 job-actions';

      // Cancel button if active
      if (['queued', 'resolving', 'downloading'].includes(job.status)) {
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'm3-btn m3-btn-outlined action-btn text-xs text-red-300 hover:text-red-200 border-red-500/30';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.setAttribute('data-action', 'cancel');
        cancelBtn.setAttribute('data-id', job.id);
        cancelBtn.addEventListener('click', () => handleJobCancel(job.id, cancelBtn));
        actionsDiv.appendChild(cancelBtn);
      }

      // Retry button if failed or partial or cancelled
      if (['failed', 'cancelled', 'blocked', 'partial'].includes(job.status)) {
        if (job.status === 'blocked' && (job.message || '').includes('Blocked: playlist contains duplicate')) {
          const contBtn = document.createElement('button');
          contBtn.type = 'button';
          contBtn.className = 'm3-btn m3-btn-outlined action-btn text-xs text-amber-300 hover:text-white border-amber-500/40';
          contBtn.textContent = 'Continue';
          contBtn.title = 'Continue downloading playlist despite duplicate tracks';
          contBtn.setAttribute('data-action', 'continue');
          contBtn.setAttribute('data-id', job.id);
          contBtn.addEventListener('click', () => handleJobContinue(job.id, contBtn));
          actionsDiv.appendChild(contBtn);
        } else {
          const retryBtn = document.createElement('button');
          retryBtn.type = 'button';
          retryBtn.className = 'm3-btn m3-btn-outlined action-btn text-xs text-teal hover:text-white border-teal/40';
          retryBtn.textContent = 'Retry';
          retryBtn.setAttribute('data-action', 'retry');
          retryBtn.setAttribute('data-id', job.id);
          retryBtn.addEventListener('click', () => handleJobRetry(job.id, retryBtn));
          actionsDiv.appendChild(retryBtn);
        }
      }

      // Remove button if terminal
      if (['completed', 'partial', 'failed', 'cancelled'].includes(job.status)) {
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'm3-btn m3-btn-outlined action-btn text-xs text-muted hover:text-white border-white/20';
        removeBtn.textContent = 'Remove';
        removeBtn.title = 'Remove job from list (downloaded songs are kept)';
        removeBtn.setAttribute('data-action', 'remove');
        removeBtn.setAttribute('data-id', job.id);
        removeBtn.setAttribute('aria-label', `Remove ${job.title || 'job'} from list (downloaded songs are kept)`);
        removeBtn.addEventListener('click', () => handleJobRemove(job.id, removeBtn));
        actionsDiv.appendChild(removeBtn);
      }

      // Details button
      const detailsBtn = document.createElement('button');
      detailsBtn.type = 'button';
      detailsBtn.className = 'm3-btn m3-btn-tonal action-btn text-xs font-medium';
      detailsBtn.textContent = activeJobId === job.id ? 'Hide Tracks' : 'View Tracks';
      detailsBtn.setAttribute('aria-expanded', activeJobId === job.id ? 'true' : 'false');
      detailsBtn.addEventListener('click', () => toggleTrackDetails(job.id));
      actionsDiv.appendChild(detailsBtn);

      metaRow.appendChild(actionsDiv);
      card.appendChild(metaRow);

      // Optional Job Message
      if (job.message) {
        const msgEl = document.createElement('div');
        msgEl.className = 'text-[11px] text-muted border-t border-white/5 pt-1.5';
        msgEl.textContent = job.message;
        card.appendChild(msgEl);
      }

      frag.appendChild(card);
    });

    jobsList.innerHTML = '';
    jobsList.appendChild(frag);
  }

  // Toggle Track Details
  async function toggleTrackDetails(jobId) {
    if (activeJobId === jobId) {
      closeTrackDetails();
      return;
    }
    activeJobId = jobId;
    await fetchTrackDetails(jobId, null, true);
  }

  function closeTrackDetails() {
    activeJobId = null;
    if (activeTrackController) {
      activeTrackController.abort();
      activeTrackController = null;
    }
    if (tracksSection) tracksSection.classList.add('hidden');
    if (tracksTbody) tracksTbody.innerHTML = '';
    if (tracksJobTitle) tracksJobTitle.textContent = '';
    if (cachedJobs.length) renderJobs(cachedJobs);
  }

  // Fetch and Render Track Details (Guarded against stale / late responses)
  async function fetchTrackDetails(jobId, signal, shouldScroll) {
    if (!tracksSection || isAccessDenied || isPageHidden) return;

    // Abort any prior in-flight track details request
    if (activeTrackController) {
      activeTrackController.abort();
      activeTrackController = null;
    }

    const controller = new AbortController();
    activeTrackController = controller;
    const fetchSignal = AbortSignal.any([controller.signal, AbortSignal.timeout(10000), ...(signal ? [signal] : [])]);

    try {
      const res = await fetch(`/api/antra/jobs/${encodeURIComponent(jobId)}`, {
        headers: { 'Accept': 'application/json' },
        signal: fetchSignal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      if (!res.ok) {
        throw new Error(`Failed to load job details (${res.status})`);
      }

      const data = await res.json();

      // Critical race condition guard: If user closed or switched while pending, ignore!
      if (activeJobId !== jobId || isAccessDenied || isPageHidden || controller.signal.aborted || (signal && signal.aborted)) {
        return;
      }

      const job = data.job || {};
      const tracks = data.tracks || [];

      // Update Section Header
      tracksJobTitle.textContent = job.title || job.url || 'Job Tracks';
      tracksJobStatusBadge.textContent = job.status || 'processing';
      tracksJobStatusBadge.className = `m3-badge ${getStatusClass(job.status)}`;

      if (tracksJobDestBadge) {
        const isFamily = job.destination === 'family';
        tracksJobDestBadge.className = `badge-destination ${isFamily ? 'badge-destination-family' : ''}`;
        tracksJobDestBadge.textContent = isFamily ? 'Family music' : 'My music (private)';
      }

      const completedCount = tracks.filter(t => t.status === 'completed').length;
      const skippedCount = tracks.filter(t => t.status === 'skipped').length;
      const failedCount = tracks.filter(t => t.status === 'failed').length;

      const summaryParts = [`${tracks.length} tracks total`, `${completedCount} completed`];
      if (skippedCount > 0) summaryParts.push(`${skippedCount} skipped`);
      if (failedCount > 0) summaryParts.push(`${failedCount} failed`);
      tracksSummary.textContent = summaryParts.join(' • ');

      // Render All Tracks into Tbody (Zero loss, proofs index 101+/250)
      const tbodyFrag = document.createDocumentFragment();

      tracks.forEach(track => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-white/[0.02] transition';

        // Column 1: Index
        const tdIdx = document.createElement('td');
        tdIdx.className = 'py-3 px-3 mono text-xs text-muted';
        tdIdx.textContent = String(typeof track.index === 'number' ? track.index + 1 : '-');
        tr.appendChild(tdIdx);

        // Column 2: Title
        const tdTitle = document.createElement('td');
        tdTitle.className = 'py-3 px-3 font-medium text-white';
        tdTitle.textContent = track.title || 'Untitled';
        tr.appendChild(tdTitle);

        // Column 3: Artist
        const tdArtist = document.createElement('td');
        tdArtist.className = 'py-3 px-3 text-soft';
        tdArtist.textContent = track.artist || 'Unknown';
        tr.appendChild(tdArtist);

        // Column 4: Audio Format (bit depth and sample rate)
        const tdFormat = document.createElement('td');
        tdFormat.className = 'py-3 px-3';
        tdFormat.appendChild(createQualityBadge(track));
        tr.appendChild(tdFormat);

        // Column 5: Status
        const tdStatus = document.createElement('td');
        tdStatus.className = 'py-3 px-3';
        const stBadge = document.createElement('span');
        stBadge.className = `badge-status ${getStatusClass(track.status)}`;
        stBadge.textContent = track.status || 'queued';
        tdStatus.appendChild(stBadge);
        tr.appendChild(tdStatus);

        // Column 6: Filename / Message
        const tdFile = document.createElement('td');
        tdFile.className = 'py-3 px-3 mono text-xs text-muted truncate max-w-xs';
        tdFile.textContent = track.filename || track.message || '-';
        tr.appendChild(tdFile);

        tbodyFrag.appendChild(tr);
      });

      tracksTbody.innerHTML = '';
      tracksTbody.appendChild(tbodyFrag);
      tracksSection.classList.remove('hidden');

      if (shouldScroll && tracksSection.scrollIntoView) {
        tracksSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }

      if (cachedJobs.length) renderJobs(cachedJobs);
    } catch (err) {
      if (err.name === 'AbortError' || activeJobId !== jobId || isAccessDenied || isPageHidden) return;
      tracksSummary.textContent = 'Error loading track list: ' + err.message;
    } finally {
      if (activeTrackController === controller) {
        activeTrackController = null;
      }
    }
  }

  // Handle URL Form Submission
  async function handleSubmit(e) {
    e.preventDefault();
    if (isSubmitting || isAccessDenied) return;

    const url = (urlInput.value || '').trim();
    if (!url) {
      showFormStatus('Please enter a valid song or playlist URL.', 'error');
      urlInput.focus();
      return;
    }

    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      showFormStatus('Invalid URL. Link must start with http:// or https://', 'error');
      urlInput.focus();
      return;
    }

    const destination = (destinationSelect && destinationSelect.value === 'family') ? 'family' : 'private';
    const isConfirming = pendingDuplicateUrl === url && pendingDuplicateDest === destination;

    // Immediate pending guard
    isSubmitting = true;
    submitBtn.disabled = true;
    submitBtn.setAttribute('aria-busy', 'true');
    submitBtnText.textContent = isConfirming ? 'Confirming…' : 'Queueing…';
    hideFormStatus();

    const postController = new AbortController();
    const timeoutId = setTimeout(() => postController.abort(), 10000);

    try {
      const payload = { url, destination };
      if (isConfirming) {
        payload.confirm_duplicate = true;
      }
      const res = await fetch('/api/antra/jobs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        body: JSON.stringify(payload),
        signal: postController.signal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      const data = await res.json();

      if (res.status === 409 && data.warning === 'duplicate') {
        // Guard against late response if user already modified inputs
        const currentUrl = (urlInput.value || '').trim();
        const currentDest = (destinationSelect && destinationSelect.value === 'family') ? 'family' : 'private';
        if (currentUrl === url && currentDest === destination) {
          pendingDuplicateUrl = url;
          pendingDuplicateDest = destination;
          submitBtnText.textContent = 'Confirm Queue';
          showFormStatus(data.message || 'Warning: duplicate detected. Click Confirm Queue to proceed.', 'warning');
        }
        return;
      }

      if (res.status === 202 || res.ok) {
        urlInput.value = '';
        pendingDuplicateUrl = null;
        pendingDuplicateDest = null;
        showFormStatus('Download queued successfully. Processing started.', 'success');
        if (pollingLoop && typeof pollingLoop.refreshAfterMutation === 'function') {
          pollingLoop.refreshAfterMutation();
        } else {
          fetchJobs();
        }
      } else {
        const errMsg = data.message || `Request failed with HTTP ${res.status}`;
        showFormStatus(errMsg, 'error');
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        showFormStatus('Request timed out. Server state uncertain; please refresh before retrying.', 'error');
      } else {
        showFormStatus('Network error: Failed to reach server. Please check your connection.', 'error');
      }
    } finally {
      clearTimeout(timeoutId);
      isSubmitting = false;
      if (!isAccessDenied) {
        submitBtn.disabled = false;
        submitBtn.removeAttribute('aria-busy');
        const hasPending = pendingDuplicateUrl === (urlInput.value || '').trim() && pendingDuplicateDest === ((destinationSelect && destinationSelect.value === 'family') ? 'family' : 'private');
        submitBtnText.textContent = hasPending ? 'Confirm Queue' : 'Queue Download';
      }
    }
  }

  // Show status message safely
  function showFormStatus(msg, type) {
    if (!formStatus) return;
    formStatus.textContent = msg;
    if (type === 'error') {
      formStatus.className = 'form-status-error text-sm mt-2';
    } else if (type === 'warning') {
      formStatus.className = 'form-status-warning text-sm mt-2';
    } else {
      formStatus.className = 'form-status-success text-sm mt-2';
    }
    formStatus.classList.remove('hidden');
  }

  function hideFormStatus() {
    if (!formStatus) return;
    formStatus.textContent = '';
    formStatus.className = 'text-sm mt-2 hidden';
  }

  // Job Continue Action
  async function handleJobContinue(jobId, btn) {
    if (!jobId || isAccessDenied) return;
    btn.disabled = true;
    btn.textContent = 'Continuing…';

    const contController = new AbortController();
    const timeoutId = setTimeout(() => contController.abort(), 10000);

    try {
      const res = await fetch(`/api/antra/jobs/${encodeURIComponent(jobId)}/continue`, {
        method: 'POST',
        headers: { 'Accept': 'application/json' },
        signal: contController.signal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      if (res.status === 202 || res.ok) {
        if (pollingLoop && typeof pollingLoop.refreshAfterMutation === 'function') {
          pollingLoop.refreshAfterMutation();
        } else {
          fetchJobs();
        }
      } else {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        alert(errData.message || 'Unable to continue job');
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        alert('Continue request timed out. Please refresh to confirm status.');
      } else {
        alert('Network error while continuing job.');
      }
    } finally {
      clearTimeout(timeoutId);
      btn.disabled = false;
      btn.textContent = 'Continue';
    }
  }

  // Job Cancel Action
  async function handleJobCancel(jobId, btn) {
    if (!jobId || isAccessDenied) return;
    btn.disabled = true;
    btn.textContent = 'Cancelling…';

    const cancelController = new AbortController();
    const timeoutId = setTimeout(() => cancelController.abort(), 10000);

    try {
      const res = await fetch(`/api/antra/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: 'POST',
        headers: { 'Accept': 'application/json' },
        signal: cancelController.signal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      if (res.status === 202 || res.ok) {
        if (pollingLoop && typeof pollingLoop.refreshAfterMutation === 'function') {
          pollingLoop.refreshAfterMutation();
        } else {
          fetchJobs();
        }
      } else {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        alert(errData.message || 'Unable to cancel job');
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        alert('Cancel request timed out. Please refresh to confirm status.');
      } else {
        alert('Network error while cancelling job.');
      }
    } finally {
      clearTimeout(timeoutId);
      btn.disabled = false;
      btn.textContent = 'Cancel';
    }
  }

  // Job Retry Action
  async function handleJobRetry(jobId, btn) {
    if (!jobId || isAccessDenied) return;
    btn.disabled = true;
    btn.textContent = 'Retrying…';

    const retryController = new AbortController();
    const timeoutId = setTimeout(() => retryController.abort(), 10000);

    try {
      const res = await fetch(`/api/antra/jobs/${encodeURIComponent(jobId)}/retry`, {
        method: 'POST',
        headers: { 'Accept': 'application/json' },
        signal: retryController.signal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      if (res.status === 202 || res.ok) {
        if (pollingLoop && typeof pollingLoop.refreshAfterMutation === 'function') {
          pollingLoop.refreshAfterMutation();
        } else {
          fetchJobs();
        }
      } else {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        alert(errData.message || 'Unable to retry job');
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        alert('Retry request timed out. Please refresh to confirm status.');
      } else {
        alert('Network error while retrying job.');
      }
    } finally {
      clearTimeout(timeoutId);
      btn.disabled = false;
      btn.textContent = 'Retry';
    }
  }

  // Job Remove Action
  async function handleJobRemove(jobId, btn) {
    if (!jobId || isAccessDenied) return;
    btn.disabled = true;
    btn.textContent = 'Removing…';

    // Clear detail view immediately if this job was currently open
    if (activeJobId === jobId) {
      closeTrackDetails();
    }

    const removeController = new AbortController();
    const timeoutId = setTimeout(() => removeController.abort(), 10000);

    try {
      const res = await fetch(`/api/antra/jobs/${encodeURIComponent(jobId)}/remove`, {
        method: 'POST',
        headers: { 'Accept': 'application/json' },
        signal: removeController.signal
      });

      if (res.status === 401 || res.status === 403) {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        handleAccessRefusal(res.status, errData.message);
        return;
      }

      if (res.status === 200 || res.status === 202 || res.ok) {
        if (pollingLoop && typeof pollingLoop.refreshAfterMutation === 'function') {
          pollingLoop.refreshAfterMutation();
        } else {
          fetchJobs();
        }
      } else {
        let errData = {};
        try { errData = await res.json(); } catch (_) {}
        alert(errData.message || 'Unable to remove job');
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        alert('Remove request timed out. Please refresh to confirm status.');
      } else {
        alert('Network error while removing job.');
      }
    } finally {
      clearTimeout(timeoutId);
      btn.disabled = false;
      btn.textContent = 'Remove';
    }
  }

  // Setup Event Listeners
  if (antraForm) antraForm.addEventListener('submit', handleSubmit);
  if (urlInput) {
    urlInput.addEventListener('input', () => {
      if (pendingDuplicateUrl !== null) {
        pendingDuplicateUrl = null;
        pendingDuplicateDest = null;
        if (submitBtnText) submitBtnText.textContent = 'Queue Download';
      }
    });
  }
  if (destinationSelect) {
    destinationSelect.addEventListener('change', () => {
      if (pendingDuplicateUrl !== null) {
        pendingDuplicateUrl = null;
        pendingDuplicateDest = null;
        if (submitBtnText) submitBtnText.textContent = 'Queue Download';
      }
    });
  }
  if (btnCloseTracks) btnCloseTracks.addEventListener('click', closeTrackDetails);
  if (btnRefresh) {
    btnRefresh.addEventListener('click', () => {
      if (pollingLoop && typeof pollingLoop.refreshAfterMutation === 'function') {
        pollingLoop.refreshAfterMutation();
      } else {
        fetchJobs();
      }
    });
  }

  // Lifecycle & Polling Setup
  function startPolling() {
    if (pollingLoop || isAccessDenied || isPageHidden) return;
    if (window.PortalPolling && typeof window.PortalPolling.createPolling === 'function') {
      pollingLoop = window.PortalPolling.createPolling((signal) => fetchJobs(signal), 4000, { name: 'antra' });
    } else {
      // If house portal helper missing, do NOT duplicate timer loop.
      // Display load failure notice and run safe single initial read.
      if (helperAlert) {
        helperAlert.textContent = 'Portal polling helper not available. Automatic background polling disabled; use Refresh button.';
        helperAlert.classList.remove('hidden');
      }
      fetchJobs();
    }
  }
  startPolling();

  // Page Lifecycle & Unload Cleanup
  window.addEventListener('pagehide', () => {
    isPageHidden = true;
    if (pollingLoop && typeof pollingLoop.dispose === 'function') {
      pollingLoop.dispose();
      pollingLoop = null;
    }
    if (activeTrackController) {
      activeTrackController.abort();
      activeTrackController = null;
    }
    if (activeMainFetchController) {
      activeMainFetchController.abort();
      activeMainFetchController = null;
    }
  });

  window.addEventListener('pageshow', () => {
    isPageHidden = false;
    startPolling();
  });

})();
