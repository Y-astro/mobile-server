(() => {
  const status = document.getElementById('laptop-status');
  const list = document.getElementById('laptop-sessions');
  const start = document.getElementById('laptop-new');
  if (!status || !list || !start) return;
  let refreshing = false;
  let changing = false;
  const endpoint = '/api/laptop/terminals';
  async function request(url, options = {}) {
    const response = await fetch(url, {credentials:'same-origin', ...options});
    if (response.status === 401 || response.status === 403) throw new Error('Administrator access required');
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || 'Laptop operation failed');
    return data;
  }
  async function refresh() {
    if (refreshing || changing) return;
    refreshing = true;
    try {
      const data = await request(endpoint);
      status.textContent = data.online ? 'Laptop connected over Tailscale' : data.error || 'Laptop offline';
      start.disabled = !data.online || data.sessions.length >= data.max_sessions;
      list.replaceChildren();
      for (const session of data.sessions) {
        if (!/^[0-9a-f]{32}$/.test(session.id)) continue;
        const row = document.createElement('div');
        row.className = 'flex items-center justify-between gap-3 p-3 rounded-xl bg-black/40';
        const label = document.createElement('span');
        label.textContent = 'Terminal ' + session.id.slice(0,8);
        const controls = document.createElement('div');
        controls.className = 'flex items-center gap-3';
        const open = document.createElement('a');
        open.textContent = 'Reconnect';
        open.href = '/laptop/?arg=' + session.id;
        open.target = '_blank'; open.rel = 'noopener';
        const close = document.createElement('button');
        close.type = 'button'; close.textContent = 'Close';
        close.setAttribute('aria-label', 'Close terminal '+session.id.slice(0,8));
        close.addEventListener('click', async () => {
          if (changing || !confirm('Close this laptop terminal and stop its commands?')) return;
          changing = true; close.disabled = true; start.disabled = true;
          try { await request(endpoint + '/' + session.id, {method:'DELETE'}); }
          catch (error) { status.textContent = error.message; }
          finally { changing = false; await refresh(); }
        });
        controls.append(open,close); row.append(label,controls); list.append(row);
      }
      if (data.online && !data.sessions.length) list.textContent = 'No laptop terminals open.';
    } catch (error) { status.textContent = error.message; start.disabled = true; }
    finally { refreshing = false; }
  }
  start.addEventListener('click', async () => {
    if (changing) return;
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
      if (!failed) await refresh();
      else start.disabled = false;
    }
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); },8000);
})();
