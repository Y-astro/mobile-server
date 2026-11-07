(() => {
  const status = document.getElementById('laptop-status');
  const list = document.getElementById('laptop-sessions');
  const start = document.getElementById('laptop-new');
  if (!status || !list || !start) return;
  let refreshing = false;
  let changing = false;
  let lastBrokerData = null;
  const endpoint = '/api/laptop/terminals';

  function isGatewayActive() {
    const toggle = document.getElementById('laptop-service-toggle');
    if (!toggle) return true;
    if (toggle.dataset.gatewayState !== undefined) {
      return toggle.dataset.gatewayState === 'running';
    }
    return toggle.checked;
  }

  function applyState() {
    if (!lastBrokerData) return;
    const data = lastBrokerData;
    const gwActive = isGatewayActive();

    if (!gwActive) {
      status.textContent = 'Laptop gateway is off (web access disabled)';
      start.disabled = true;
    } else {
      status.textContent = data.online ? 'Laptop connected over Tailscale' : data.error || 'Laptop offline';
      start.disabled = !data.online || data.sessions.length >= data.max_sessions;
    }

    list.replaceChildren();
    for (const session of data.sessions) {
      if (!/^[0-9a-f]{32}$/.test(session.id)) continue;
      const row = document.createElement('div');
      row.className = 'flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-3 rounded-xl bg-surface-raised border border-white/5';
      const label = document.createElement('span');
      label.className = 'font-medium text-white mono text-xs';
      label.textContent = 'Terminal ' + session.id.slice(0,8);
      const controls = document.createElement('div');
      controls.className = 'flex items-center gap-2';
      const open = document.createElement('a');
      open.className = 'm3-btn m3-btn-tonal text-xs font-medium' + (!gwActive ? ' opacity-40 pointer-events-none' : '');
      open.textContent = 'Reconnect';
      if (gwActive) {
        open.href = '/laptop/?arg=' + session.id;
        open.target = '_blank'; open.rel = 'noopener';
      } else {
        open.setAttribute('aria-disabled', 'true');
        open.title = 'Gateway is off';
      }
      const close = document.createElement('button');
      close.className = 'm3-btn m3-btn-outlined text-xs font-medium';
      close.type = 'button'; close.textContent = 'Close';
      close.setAttribute('aria-label', 'Close terminal '+session.id.slice(0,8));
      close.addEventListener('click', async () => {
        if (changing || !confirm('Close this laptop terminal and stop its commands?')) return;
        changing = true; close.disabled = true; start.disabled = true;
        try { await request(endpoint + '/' + session.id, {method:'DELETE'}); }
        catch (error) { status.textContent = error.message; }
        finally { changing = false; await (laptopLoop ? laptopLoop.refreshAfterMutation() : refresh()); }
      });
      controls.append(open,close); row.append(label,controls); list.append(row);
    }
    if (data.online && !data.sessions.length) {
      list.textContent = gwActive ? 'No laptop terminals open.' : 'No laptop terminals open (Gateway Off).';
    }
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {credentials:'same-origin', ...options});
    if (response.status === 401 || response.status === 403) throw new Error('Administrator access required');
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || 'Laptop operation failed');
    return data;
  }
  async function refresh(signal) {
    if (refreshing || changing) return;
    refreshing = true;
    try {
      const data = await request(endpoint, signal ? { signal } : {});
      if (signal && signal.aborted) return;
      lastBrokerData = data;
      applyState();
    } catch (error) {
      if (signal && signal.aborted && (signal.reason === 'lifecycle_pause' || signal.reason === 'lifecycle_dispose')) return;
      status.textContent = error.message;
      start.disabled = true;
    } finally { refreshing = false; }
  }

  window.addEventListener('laptop-gateway-state', () => {
    applyState();
  });

  start.addEventListener('click', async () => {
    if (changing || !isGatewayActive()) return;
    changing = true; start.disabled = true;
    // Open on the click gesture; otherwise browsers block a tab opened after SSH.
    const terminal = window.open('about:blank', '_blank');
    if (terminal) terminal.opener = null;
    let failed = false;
    try {
      const data = await request(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      if (!/^[0-9a-f]{32}$/.test(data.id)) throw new Error('Laptop returned an invalid terminal ID');
      if (terminal) terminal.location.href = '/laptop/?arg=' + data.id;
    } catch (error) {
      failed = true;
      if (terminal) terminal.close();
      status.textContent = error.message;
    } finally {
      changing = false;
      // Preserve the actual operation error until the next periodic status check.
      if (!failed) await (laptopLoop ? laptopLoop.refreshAfterMutation() : refresh());
      else start.disabled = false;
    }
  });

  const laptopLoop = (typeof window !== 'undefined' && window.PortalPolling && typeof window.PortalPolling.createPolling === 'function')
    ? window.PortalPolling.createPolling((signal) => refresh(signal), 8000, { name: 'laptop' })
    : null;

  if (!laptopLoop) {
    const fallbackCtrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const fallbackTimer = fallbackCtrl ? setTimeout(() => fallbackCtrl.abort('deadline_timeout'), 10000) : null;
    refresh(fallbackCtrl ? fallbackCtrl.signal : null).finally(() => {
      if (fallbackTimer) clearTimeout(fallbackTimer);
    });
  }
})();
