/* Shared matching rules, also exercised directly by Node regression tests. */
(() => {
  'use strict';
  const fold = value => String(value ?? '').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
  const facetValue = (record, key) => key === 'Rep date' && /^\d{4}-/.test(record[key]) ? record[key].slice(0, 4) : record[key] || '';
  function matches(record, query, selections) {
    const words = fold(query).split(' ').filter(Boolean);
    const text = fold(Object.values(record).join(' '));
    return words.every(word => text.includes(word)) && Object.entries(selections).every(([key, values]) => !values.size || values.has(fold(facetValue(record, key))));
  }
  if (typeof module !== 'undefined') module.exports = { fold, facetValue, matches };
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const selections = {};
  const collator = new Intl.Collator(undefined, { sensitivity: 'base', numeric: true });
  let catalogue, visible = 50, loading = false, changing = false, versions;
  const filterKeys = ['rAga', 'tALa', 'Composer', 'Comp_type', 'Language', 'Subject', 'Repertoire', 'pATAntara', 'ConcertFit', 'Teaching', 'Rep date'];
  const labels = {};
  const optionLabels = {};
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function closeFilters() {
    document.querySelectorAll('.facet[open]').forEach(node => { node.open = false; });
  }
  function buildFilters() {
    $('filters').replaceChildren();
    for (const key of filterKeys) {
      const options = new Map();
      for (const record of catalogue.records) {
        const raw = facetValue(record, key), normalized = fold(raw);
        if (!options.has(normalized)) options.set(normalized, raw || 'Not entered');
      }
      optionLabels[key] = options;
      selections[key] ??= new Set();
      const box = element('details', undefined, 'facet');
      const summary = element('summary');
      summary.append(element('span', labels[key]), element('span', '', 'badge'));
      box.append(summary);
      const panel = element('div', undefined, 'facet-panel');
      const search = element('input');
      search.type = 'search'; search.placeholder = 'Find a value…';
      search.setAttribute('aria-label', 'Search ' + labels[key] + ' values');
      panel.append(search);
      const list = element('div', undefined, 'facet-options');
      const sorted = [...options].sort((a, b) => a[0] === '' ? -1 : b[0] === '' ? 1 : collator.compare(a[1], b[1]));
      for (const [value, label] of sorted) {
        const option = element('label', undefined, 'facet-option');
        const input = element('input'); input.type = 'checkbox'; input.value = value;
        input.checked = selections[key].has(value);
        input.addEventListener('change', () => {
          if (input.checked) selections[key].add(value); else selections[key].delete(value);
          visible = 50; render();
        });
        option.append(input, element('span', label)); list.append(option);
      }
      const empty = element('p', 'No values match your search.', 'facet-empty'); empty.hidden = true;
      search.addEventListener('input', () => {
        let count = 0;
        for (const option of list.children) {
          option.hidden = !fold(option.textContent).includes(fold(search.value));
          if (!option.hidden) count++;
        }
        empty.hidden = count > 0;
      });
      panel.append(list, empty); box.append(panel);
      box.addEventListener('toggle', () => {
        if (box.open) {
          for (const other of $('filters').children) if (other !== box) other.open = false;
        }
      });
      box.dataset.key = key; $('filters').append(box);
    }
  }
  function showDetails(record) {
    $('detail-title').textContent = record.Composition;
    $('detail-fields').replaceChildren(element('dt', 'Catalogue no.'), element('dd', record.id));
    for (const { key, label } of catalogue.fields) {
      if (key === 'Composition') continue;
      const value = record[key];
      const description = element('dd', value || 'Not entered');
      if (key === 'Reference' && value) {
        try {
          const url = new URL(value.startsWith('www.') ? 'https://' + value : value);
          if (['http:', 'https:'].includes(url.protocol)) {
            const link = element('a', value); link.href = url.href;
            link.target = '_blank'; link.rel = 'noopener noreferrer'; description.replaceChildren(link);
          }
        } catch { /* Plain references such as CD names stay readable text. */ }
      }
      $('detail-fields').append(element('dt', label), description);
    }
    $('details').showModal();
  }
  function render() {
    if (!catalogue) return;
    const rows = catalogue.records.filter(record => matches(record, $('search').value, selections));
    const key = { title: 'Composition', raga: 'rAga', composer: 'Composer' }[$('sort').value];
    rows.sort((a, b) => collator.compare(a[key], b[key]) || collator.compare(a.Composition, b.Composition) || collator.compare(a.id, b.id));
    $('result-count').textContent = rows.length.toLocaleString() + ' of ' + catalogue.records.length.toLocaleString() + ' compositions';
    $('selected').replaceChildren();
    for (const [field, values] of Object.entries(selections)) {
      const box = [...$('filters').children].find(node => node.dataset.key === field);
      if (box) {
        const badge = box.querySelector('.badge'); badge.textContent = String(values.size); badge.hidden = !values.size;
        box.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = values.has(input.value); });
      }
      for (const value of values) {
        const label = optionLabels[field]?.get(value) ?? (value || 'Not entered');
        const chip = element('button', labels[field] + ': ' + label + ' ×', 'filter-chip'); chip.type = 'button';
        chip.setAttribute('aria-label', 'Remove ' + labels[field] + ': ' + label);
        chip.addEventListener('click', () => { values.delete(value); visible = 50; render(); });
        $('selected').append(chip);
      }
    }
    const fragment = document.createDocumentFragment();
    for (const record of rows.slice(0, visible)) {
      const card = element('button', undefined, 'song-card'); card.type = 'button'; card.dataset.id = record.id;
      card.append(element('span', record.Composition, 'song-title'));
      card.append(element('span', (record.rAga || 'Raga not entered') + ' · ' + (record.tALa || 'Tala not entered'), 'song-meta'));
      card.append(element('span', record.Composer || 'Composer not entered', 'song-meta'));
      const tags = element('span', undefined, 'song-bottom');
      for (const field of ['Comp_type', 'Language', 'Repertoire']) {
        if (record[field]) tags.append(element('span', (field === 'Repertoire' ? 'Repertoire: ' : '') + record[field], 'song-tag'));
      }
      card.append(tags); card.addEventListener('click', () => showDetails(record)); fragment.append(card);
    }
    if (!rows.length) fragment.append(element('div', 'No compositions match these selections. Remove a filter or clear all to broaden your search.', 'empty-results'));
    $('results').replaceChildren(fragment);
    $('more').hidden = rows.length <= visible;
    $('more').textContent = 'Show more · ' + Math.min(visible, rows.length) + ' of ' + rows.length;
  }
  function showVersions(next) {
    versions = next;
    $('workbook-version').value = next.active;
    const previous = $('workbook-version').querySelector('[value="previous"]');
    previous.disabled = !next.previous;
    previous.textContent = next.previous ? 'Previous' : 'Previous — no earlier upload';
  }
  function setBusy(busy) {
    for (const id of ['refresh', 'workbook-version', 'open-upload', 'workbook-file', 'cancel-upload', 'submit-upload']) $(id).disabled = busy;
    $('submit-upload').textContent = busy ? 'Checking and uploading…' : 'Upload and use Current';
    $('upload-form').setAttribute('aria-busy', String(busy));
  }
  async function changeWorkbook(path, body) {
    const response = await fetch('/api/repertoire/' + path, {
      method: 'POST', credentials: 'same-origin', headers: { 'X-Repertoire-Request': '1', 'Content-Type': path === 'upload' ? 'application/octet-stream' : 'application/json' }, body,
    });
    if (response.status === 401) { location.assign('/login'); throw new Error('Please sign in again.'); }
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'The workbook could not be changed. Refresh and retry.');
    showVersions(result.versions);
    $('workbook-status').textContent = result.message; $('workbook-status').hidden = false;
  }
  async function load() {
    if (loading) return;
    loading = true; $('refresh').disabled = true; $('workbook-version').disabled = true; $('error').hidden = true;
    try {
      const response = await fetch('/api/repertoire', { credentials: 'same-origin', cache: 'no-store' });
      if (response.status === 401) { location.assign('/login'); return; }
      if (!response.ok) throw new Error('The workbook could not be loaded. Check that the repertoire file in FamilyShare has a valid Data sheet, then refresh.');
      const next = await response.json();
      showVersions(next.versions);
      if (!catalogue || next.revision !== catalogue.revision) {
        catalogue = next;
        for (const field of next.fields) labels[field.key] = field.key === 'Rep date' ? 'Repertoire year' : field.label;
        buildFilters(); visible = 50; render();
      }
      $('source-info').textContent = next.versions.active.charAt(0).toUpperCase() + next.versions.active.slice(1) + ' workbook · Updated ' + new Date(next.updated).toLocaleDateString();
    } catch (error) {
      catalogue = null; $('filters').replaceChildren(); $('results').replaceChildren(); $('selected').replaceChildren(); $('more').hidden = true;
      $('result-count').textContent = 'Repertoire unavailable'; $('source-info').textContent = '';
      $('error').textContent = error.message; $('error').hidden = false;
      try {
        const metadata = await fetch('/api/repertoire/versions', { credentials: 'same-origin', cache: 'no-store' });
        if (metadata.ok) showVersions(await metadata.json());
      } catch { /* Upload remains available when the stored workbook needs replacing. */ }
    } finally { loading = false; if (!changing) setBusy(false); }
  }
  $('open-upload').addEventListener('click', () => {
    $('upload-form').reset(); $('upload-error').hidden = true; $('upload-dialog').showModal();
  });
  $('cancel-upload').addEventListener('click', () => $('upload-dialog').close());
  $('upload-dialog').addEventListener('cancel', event => { if (changing) event.preventDefault(); });
  $('upload-form').addEventListener('submit', async event => {
    event.preventDefault();
    const file = $('workbook-file').files[0];
    if (!file || changing || loading) return;
    $('upload-error').hidden = true; $('workbook-status').hidden = true;
    if (!/\.xlsx$/i.test(file.name) || !file.size || file.size > 8 * 1024 * 1024) {
      $('upload-error').textContent = 'Choose an Excel (.xlsx) workbook up to 8 MB.'; $('upload-error').hidden = false; return;
    }
    changing = true; setBusy(true);
    try {
      await changeWorkbook('upload', file);
      $('upload-dialog').close(); await load();
    } catch (error) {
      $('upload-error').textContent = error.message; $('upload-error').hidden = false;
    } finally { changing = false; setBusy(false); }
  });
  $('workbook-version').addEventListener('change', async () => {
    if (changing || loading) return;
    changing = true; setBusy(true); $('workbook-status').hidden = true;
    try { await changeWorkbook('select', JSON.stringify({ version: $('workbook-version').value })); }
    catch (error) { $('workbook-status').textContent = error.message; $('workbook-status').hidden = false; }
    finally { await load(); changing = false; setBusy(false); }
  });
  $('search').addEventListener('input', () => { visible = 50; render(); });
  $('sort').addEventListener('change', () => { visible = 50; render(); });
  $('more').addEventListener('click', () => { visible += 50; render(); });
  $('reset').addEventListener('click', () => {
    for (const values of Object.values(selections)) values.clear();
    $('search').value = ''; visible = 50; closeFilters(); render();
  });
  $('refresh').addEventListener('click', load);
  $('close-details').addEventListener('click', () => $('details').close());
  $('details').setAttribute('aria-labelledby', 'detail-title');
  document.addEventListener('click', event => { if (!event.target.closest('.facet')) closeFilters(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeFilters(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !changing) load(); });
  setInterval(() => { if (!document.hidden && !changing) load(); }, 60000);
  load();
})();
