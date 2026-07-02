/* ═══════════════════════════════════════════════════════════════════════════
   S&K Route Dispatch — dashboard frontend (v2)
   ═══════════════════════════════════════════════════════════════════════════ */

const STATE = {
  clients: [],
  runPollTimer: null,
  lastPlanToday: null,
  solveBudgetSeconds: null,   // captured when the user clicks Run
  progressFadeTimer: null,
};

const $ = (sel) => document.querySelector(sel);

window.addEventListener('DOMContentLoaded', () => {
  $('#run-btn').addEventListener('click', onRunClick);
  $('#browse-btn').addEventListener('click', onBrowseClick);
  $('#client-search').addEventListener('input', renderClients);
  $('#client-sort').addEventListener('change', renderClients);
  $('#client-filter').addEventListener('change', renderClients);
  // Re-render clients + truck widget when the planning date changes so
  // the "Manual override (for Fri May 22)" label + commit-window stay in sync.
  $('#plan-date').addEventListener('change', () => {
    renderClients();
    refreshTrucks();
  });
  // Solver Tuning — sliders
  $('#min-fill-pct').addEventListener('input', onMinFillSliderInput);
  $('#min-fill-pct').addEventListener('change', onMinFillSliderCommit);   // fires on mouseup
  // Urgency Matrix modal
  $('#urgency-btn').addEventListener('click', openUrgencyMatrix);
  // Deliveries In Progress card — null-safe bindings: if the served HTML
  // is older than this script (stale template / browser cache), skip the
  // card quietly instead of crashing the whole dashboard load.
  const bind = (id, evt, fn) => { const el = $(id); if (el) el.addEventListener(evt, fn); };
  if ($('#ip-date')) $('#ip-date').value = todayISO();
  bind('#ip-search', 'input', renderIpSuggestions);
  bind('#ip-search', 'focus', renderIpSuggestions);
  bind('#ip-log-btn', 'click', openIpLog);
  bind('#ip-copy-ab', 'click', () => copyIp('ab'));
  bind('#ip-copy-d', 'click', () => copyIp('d'));
  bind('#ip-log-search', 'input', ipLogSearchInput);
  bind('#ip-log-search', 'keydown', ipLogSearchKeydown);
  document.addEventListener('click', (ev) => {
    const sug = $('#ip-suggestions');
    if (sug && !ev.target.closest('.card-inprogress')) sug.hidden = true;
  });
  refreshAll();
});

async function refreshAll() {
  await Promise.all([
    refreshHealthStrip(),
    refreshLastPlan(),
    refreshClients(),
    refreshTrucks(),
    refreshSolverSettings(),
    refreshInProgress(),
  ]);
}

// ── Deliveries In Progress (assumed-delivered sidecar) ──────────────────────

async function refreshInProgress() {
  if (!$('#ip-list')) return;              // card not in served HTML — skip
  try {
    const r = await fetch('/api/in-progress');
    const data = await r.json();
    STATE.inProgress = data.entries || [];
    renderInProgress(data.expiry_days);
  } catch (e) { /* card stays in last state */ }
}

function clientName(cid) {
  const c = STATE.clients.find(x => x.id === cid);
  return c ? displayName(c.name) : cid;
}

function renderInProgress(expiryDays) {
  const list = $('#ip-list');
  const entries = STATE.inProgress || [];
  $('#ip-count').textContent = entries.length
    ? `${entries.filter(e => e.status === 'active').length} active`
    : '—';
  if (!entries.length) {
    list.innerHTML = '<div class="empty">None — add today\'s dispatched stops here.</div>';
    return;
  }
  const hasExpired = entries.some(e => e.status === 'expired');
  list.innerHTML = entries.map(e => {
    const cls = e.status === 'expired' ? 'ip-row ip-expired' : 'ip-row';
    const badge = e.status === 'expired'
      ? `<span class="ip-badge" title="Never appeared in the Delivery_Log within ${expiryDays} days — the optimizer now IGNORES this entry. Log the real delivery or remove it.">expired</span>`
      : '';
    const qty = e.qty_lbs ? ` · ${Math.round(e.qty_lbs)} lbs` : '';
    return `
      <div class="${cls}">
        <span class="ip-name" title="${escape(e.client_id)}">${escape(clientName(e.client_id))}</span>
        <span class="ip-meta">${escape(e.date)}${qty}</span>
        ${badge}
        <button class="btn-mini-flat ip-remove" title="Remove — the optimizer will treat this client as NOT delivered"
                onclick="removeInProgress('${escape(e.client_id)}', '${escape(e.date)}')">✕</button>
      </div>`;
  }).join('') + (hasExpired
    ? `<button class="btn-mini-flat ip-clear" onclick="clearExpiredInProgress()">Clear expired</button>`
    : '');
}

function renderIpSuggestions() {
  const box = $('#ip-suggestions');
  const q = $('#ip-search').value.trim().toLowerCase();
  if (q.length < 2) { box.hidden = true; return; }
  const existing = new Set((STATE.inProgress || []).map(e => e.client_id));
  const matches = STATE.clients
    .filter(c => !existing.has(c.id) &&
                 (c.name.toLowerCase().includes(q) || c.id.toLowerCase().includes(q)))
    .slice(0, 8);
  if (!matches.length) { box.hidden = true; return; }
  box.innerHTML = matches.map(c =>
    `<div class="ip-suggestion" onclick="addInProgress('${escape(c.id)}')">
       <b>${escape(c.id)}</b> ${escape(displayName(c.name))}
     </div>`).join('');
  box.hidden = false;
}

async function addInProgress(cid) {
  $('#ip-suggestions').hidden = true;
  $('#ip-search').value = '';
  try {
    const r = await fetch('/api/in-progress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'add', client_ids: [cid],
                             date: $('#ip-date').value || todayISO() }),
    });
    if ((await r.json()).ok) refreshInProgress();
  } catch (e) { /* leave as-is */ }
}

async function removeInProgress(cid, d) {
  try {
    const r = await fetch('/api/in-progress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'remove', client_id: cid, date: d }),
    });
    if ((await r.json()).ok) refreshInProgress();
  } catch (e) { /* leave as-is */ }
}

async function clearExpiredInProgress() {
  try {
    const r = await fetch('/api/in-progress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'clear-expired' }),
    });
    if ((await r.json()).ok) refreshInProgress();
  } catch (e) { /* leave as-is */ }
}

// ── Log Amounts modal (qty entry + Delivery_Log paste export) ───────────────

function openIpLog() {
  $('#ip-log-search').value = '';
  renderIpLog();
  $('#ip-log-modal').hidden = false;
  setTimeout(() => $('#ip-log-search').focus(), 50);
}
function closeIpLog() { $('#ip-log-modal').hidden = true; }
function closeIpLogIfBackdrop(ev) {
  if (ev.target === $('#ip-log-modal')) closeIpLog();
}

function ipLogVisibleEntries() {
  const q = ($('#ip-log-search').value || '').trim().toLowerCase();
  let entries = (STATE.inProgress || []).filter(e => e.status !== 'future');
  if (q) {
    entries = entries.filter(e =>
      e.client_id.toLowerCase().includes(q) ||
      clientName(e.client_id).toLowerCase().includes(q));
  }
  // Unfilled amounts first — they're the ones being worked through.
  return entries.sort((a, b) =>
    (a.qty_lbs == null ? 0 : 1) - (b.qty_lbs == null ? 0 : 1));
}

function renderIpLog() {
  const body = $('#ip-log-body');
  const entries = ipLogVisibleEntries();
  const all = (STATE.inProgress || []).filter(e => e.status !== 'future');
  const done = all.filter(e => e.qty_lbs != null).length;
  if (!all.length) {
    body.innerHTML = '<div class="empty">No in-progress deliveries.</div>';
    return;
  }
  if (!entries.length) {
    body.innerHTML = '<div class="empty">No match — check spelling or clear the search.</div>';
    return;
  }
  body.innerHTML = `
    <div class="ip-log-progress">${done} of ${all.length} amounts entered</div>
    <table class="ip-log-table">
      <thead><tr><th>Date</th><th>ID</th><th>Client</th><th>Qty delivered (lbs)</th></tr></thead>
      <tbody>
        ${entries.map(e => `
          <tr class="${e.qty_lbs != null ? 'ip-log-done' : ''}">
            <td>${escape(e.date)}</td>
            <td class="name-id">${escape(e.client_id)}</td>
            <td class="ip-log-name">${escape(clientName(e.client_id))}</td>
            <td><input type="number" min="0" step="1" class="ip-qty"
                       value="${e.qty_lbs != null ? Math.round(e.qty_lbs) : ''}"
                       placeholder="—"
                       data-cid="${escape(e.client_id)}" data-date="${escape(e.date)}"
                       onchange="setIpQty(this)"
                       onkeydown="ipQtyKeydown(event, this)"></td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

// Search box: typing filters; Enter jumps to the first visible amount field.
function ipLogSearchInput() { renderIpLog(); }
function ipLogSearchKeydown(ev) {
  if (ev.key !== 'Enter') return;
  ev.preventDefault();
  const first = $('#ip-log-body').querySelector('.ip-qty');
  if (first) { first.focus(); first.select(); }
}

// Amount field: Enter saves, then returns to the search box (cleared) so the
// next customer can be typed immediately — same rhythm as adding stops.
async function ipQtyKeydown(ev, input) {
  if (ev.key !== 'Enter') return;
  ev.preventDefault();
  await setIpQty(input);
  const search = $('#ip-log-search');
  search.value = '';
  renderIpLog();
  search.focus();
}

async function setIpQty(input) {
  const qty = input.value === '' ? null : parseFloat(input.value);
  try {
    await fetch('/api/in-progress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'set-qty', client_id: input.dataset.cid,
                             date: input.dataset.date, qty_lbs: qty }),
    });
    await refreshInProgress();          // card shows the qty too
  } catch (e) { /* leave as-is */ }
}

function ipLoggedRows() {
  // Only rows with a qty, in Delivery_Log append order (oldest first).
  return (STATE.inProgress || [])
    .filter(e => e.qty_lbs != null && e.status !== 'future')
    .sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 :
                    a.client_id < b.client_id ? -1 : 1);
}

function fmtLogDate(iso) {           // 2026-07-02 → 07/02/2026 (Excel-friendly)
  const [y, m, d] = iso.split('-');
  return `${m}/${d}/${y}`;
}

async function copyIp(mode) {
  const rows = ipLoggedRows();
  const status = $('#ip-copy-status');
  if (!rows.length) { status.textContent = 'No quantities entered yet.'; return; }
  const text = rows.map(e => mode === 'ab'
    ? `${fmtLogDate(e.date)}\t${e.client_id}`
    : `${Math.round(e.qty_lbs)}`).join('\n');
  try {
    await navigator.clipboard.writeText(text);
    status.textContent = mode === 'ab'
      ? `${rows.length} rows copied — paste at the first empty cell in column A.`
      : `${rows.length} quantities copied — paste at the matching cell in column D.`;
  } catch (e) {
    status.textContent = 'Copy failed — use the CSV download instead.';
  }
}

// ── Solver Tuning ─────────────────────────────────────────────────────────
async function refreshSolverSettings() {
  try {
    const r = await fetch('/api/settings/solver');
    const s = await r.json();
    const pct = Math.round((s.min_fill_pct ?? 0.5) * 100);
    $('#min-fill-pct').value = pct;
    $('#min-fill-pct-val').textContent = pct + '%';
    setMinFillHint('');
  } catch (e) {
    setMinFillHint('Could not load: ' + e, 'error');
  }
}

function onMinFillSliderInput() {
  // Live update of the displayed value as the user drags
  const pct = parseInt($('#min-fill-pct').value, 10);
  $('#min-fill-pct-val').textContent = pct + '%';
  setMinFillHint(describeMinFill(pct), '');
}

async function onMinFillSliderCommit() {
  const pct = parseInt($('#min-fill-pct').value, 10);
  setMinFillHint('Saving…', 'saving');
  try {
    const r = await fetch('/api/settings/solver', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ min_fill_pct: pct / 100 }),
    });
    const res = await r.json();
    if (!res.ok) { setMinFillHint(res.error || 'Save failed', 'error'); return; }
    setMinFillHint('Saved — applies on next Run.', 'saved');
  } catch (e) {
    setMinFillHint('Network error: ' + e, 'error');
  }
}

function describeMinFill(pct) {
  if (pct === 0) return 'Disabled — solver may schedule any-size top-off.';
  if (pct < 30)  return `Permissive — only filters very small top-offs.`;
  if (pct < 50)  return `Light filter — small top-offs may still be planned.`;
  if (pct < 65)  return `Standard — skip stops with high tank headroom.`;
  return `Aggressive — only big refills (urgent clients still served).`;
}

function setMinFillHint(text, cls) {
  const el = $('#min-fill-pct-hint');
  el.textContent = text;
  el.className = 'tuning-hint' + (cls ? ' ' + cls : '');
}


// ── Data Strip (top compact stats + file path) ────────────────────────────
async function refreshHealthStrip() {
  try {
    const r = await fetch('/api/health');
    const h = await r.json();

    if (!h.ok) {
      $('#strip-file').textContent = h.error || 'No file';
      $('#strip-file').title = h.error || '';
      $('#stat-mtime').innerHTML = '';
      $('#stat-deliv').innerHTML = '';
      $('#stat-anova').innerHTML = '';
      $('#stat-clients').innerHTML = '';
      return;
    }

    // Show just the FILENAME — the SharePoint path is too long for the strip.
    // Full path remains in the tooltip on hover.
    const filename = (h.path || '').split('/').pop() || '(no name)';
    const where = h.path && h.path.includes('CloudStorage') ? 'OneDrive'
                : h.path && h.path.includes('Downloads')   ? 'Downloads'
                : 'local';
    $('#strip-file').innerHTML =
      `<b>${escape(filename)}</b> <span style="color:var(--text-faint)">— ${escape(where)}</span>`;
    $('#strip-file').title = h.path;

    // FILE FRESHNESS
    const age = h.age_hours;
    const ageClass = age < 24 ? 'ok' : age < 72 ? 'warn' : 'danger';
    const ageLabel = age < 1 ? `${Math.round(age*60)} min`
                    : age < 24 ? `${Math.round(age)} h`
                    : `${(age/24).toFixed(1)} d`;
    $('#stat-mtime').innerHTML =
      `<span class="stat-key">File saved</span><span class="stat-val ${ageClass}">${ageLabel} ago</span>`;

    // LAST DELIVERY LOGGED — humanized
    let ld;
    if (h.last_delivery == null) {
      ld = 'unknown';
    } else if (h.last_delivery_days_ago === 0) {
      ld = `today`;
    } else if (h.last_delivery_days_ago === 1) {
      ld = `yesterday (${h.last_delivery})`;
    } else {
      ld = `${h.last_delivery_days_ago} d ago (${h.last_delivery})`;
    }
    const ldClass = (h.last_delivery_days_ago ?? 99) <= 2 ? 'ok' : 'warn';
    $('#stat-deliv').innerHTML =
      `<span class="stat-key">Last delivery logged</span><span class="stat-val ${ldClass}">${ld}</span>`;

    // ANOVA — "newest reading: X ago" instead of cryptic "-6.9h"
    const anovaPct = h.anova_pct || 0;
    const anovaClass = anovaPct >= 50 ? 'ok' : anovaPct >= 25 ? 'warn' : 'danger';
    let anovaFresh = '';
    if (h.anova_freshest_hours != null) {
      const a = Math.abs(h.anova_freshest_hours);
      const aLbl = a < 1 ? `${Math.round(a*60)} min`
                   : a < 24 ? `${Math.round(a)} h`
                   : `${(a/24).toFixed(1)} d`;
      anovaFresh = ` <span style="color:var(--text-faint)">— newest reading ${aLbl} ago</span>`;
    }
    $('#stat-anova').innerHTML =
      `<span class="stat-key">Anova sensors reporting</span><span class="stat-val ${anovaClass}">${h.anova_with_reading || 0} of ${h.anova_total || 0} (${anovaPct}%)</span>${anovaFresh}`;

    $('#stat-clients').innerHTML =
      `<span class="stat-key">Clients</span><span class="stat-val">${h.client_count || 0} active <span style="color:var(--text-faint)">(${h.client_dns || 0} do-not-schedule)</span></span>`;

    // Render any data-freshness warnings
    renderWarnings(h.warnings || []);
  } catch (e) {
    $('#strip-file').textContent = `Error: ${e}`;
  }
}

function renderWarnings(warnings) {
  const banner = $('#warn-banner');
  if (!warnings.length) { banner.hidden = true; banner.innerHTML = ''; return; }
  banner.hidden = false;
  const icon = { block: '⛔', warn: '⚠️', info: 'ℹ️' };
  banner.innerHTML = warnings.map(w =>
    `<div class="warn-line ${w.severity === 'block' ? 'block' : ''}">
       <span class="warn-icon">${icon[w.severity] || '·'}</span>
       <span class="warn-msg">${escape(w.message)}</span>
     </div>`).join('');
}

// ── File picker ───────────────────────────────────────────────────────────
async function onBrowseClick() {
  $('#browse-btn').disabled = true;
  $('#browse-btn').textContent = '⏳ Choose…';
  try {
    const r = await fetch('/api/pick-file', { method: 'POST' });
    const res = await r.json();
    if (res.ok && res.path) {
      // success — refresh everything
      await refreshAll();
    } else if (!res.cancelled) {
      alert('Could not pick file: ' + (res.error || 'unknown error'));
    }
  } catch (e) {
    alert('Error: ' + e);
  } finally {
    $('#browse-btn').disabled = false;
    $('#browse-btn').textContent = '📂 Browse';
  }
}

// ── Last Plan card (KPIs + downloads + committed/preview days) ────────────
async function refreshLastPlan() {
  try {
    const r = await fetch('/api/last-plan');
    const p = await r.json();
    if (!p.ok) {
      $('#plan-body').innerHTML = `<div class="empty">No plan yet — click <b>Run</b> to build one.</div>`;
      $('#plan-stamp').textContent = '';
      $('#horizon-days').textContent = '10';
      $('#commit-days').textContent = '2';
      $('#urgency-btn').disabled = true;
      return;
    }
    STATE.lastPlanToday = p.today;
    $('#urgency-btn').disabled = false;

    $('#horizon-days').textContent = p.horizon_days;
    $('#commit-days').textContent = p.commit_days;

    const html = [];
    // KPIs
    html.push(`<div class="kpi-grid">
      <div class="kpi"><div class="kpi-label">Stops</div><div class="kpi-value">${p.total_stops ?? '—'}</div></div>
      <div class="kpi"><div class="kpi-label">Lbs</div><div class="kpi-value">${p.total_lbs != null ? Math.round(p.total_lbs).toLocaleString() : '—'}</div></div>
      <div class="kpi"><div class="kpi-label">Miles</div><div class="kpi-value">${p.total_miles != null ? Math.round(p.total_miles).toLocaleString() : '—'}</div></div>
      <div class="kpi"><div class="kpi-label">Fill %</div><div class="kpi-value">${p.avg_fill != null ? Math.round(p.avg_fill)+'%' : '—'}</div></div>
    </div>`);

    // Downloads — use the file paths the BACKEND confirmed actually exist.
    // Excel + CSV use `download` attr → browser saves the file (otherwise
    // Chrome shows an empty tab because it doesn't know how to render .xlsx).
    // Map opens in a new tab (it's a self-contained interactive HTML).
    const files = p.files || {};
    const dl = [];
    if (files.excel) {
      dl.push(`<a class="download-link" href="/outputs/${files.excel}" download>
        <span>📊 Plan workbook (Excel)</span><span class="download-icon">↓</span></a>`);
    }
    if (files.map) {
      dl.push(`<a class="download-link" href="/outputs/${files.map}" target="_blank" rel="noopener">
        <span>🗺️  Route map (opens in new tab)</span><span class="download-icon">↗</span></a>`);
    }
    if (files.csv) {
      dl.push(`<a class="download-link" href="/outputs/${files.csv}" download>
        <span>📤 SmartService manifest (CSV)</span><span class="download-icon">↓</span></a>`);
    }
    if (dl.length === 0) {
      dl.push('<div class="empty">No output files found in final/output/</div>');
    }
    html.push(`<div class="downloads">${dl.join('')}</div>`);

    // Committed days
    if (p.committed && p.committed.length) {
      html.push(`<div class="section-label firm">Firm — dispatch these</div>`);
      html.push('<div class="day-list">');
      for (const d of p.committed) html.push(dayBlockHTML(d, false));
      html.push('</div>');
    }
    // Preview days
    if (p.preview && p.preview.length) {
      html.push(`<div class="section-label preview">Preview — will re-solve</div>`);
      html.push('<div class="day-list">');
      for (const d of p.preview) html.push(dayBlockHTML(d, true));
      html.push('</div>');
    }

    html.push(`<span class="log-link" onclick="document.getElementById('run-log-modal').hidden=false">View run log →</span>`);

    $('#plan-body').innerHTML = html.join('');
    $('#plan-stamp').textContent = p.generated_at
      ? `${new Date(p.generated_at).toLocaleString()} · ${p.solve_seconds}s`
      : '';
  } catch (e) {
    $('#plan-body').innerHTML = `<div class="empty">Error: ${escape(String(e))}</div>`;
  }
}

function dayBlockHTML(d, isPreview) {
  const totalStops = d.trucks.reduce((s,t)=>s+t.stops, 0);
  const totalLbs = d.trucks.reduce((s,t)=>s+t.lbs, 0);
  const truckLine = d.trucks.map(t => {
    const t_class = t.truck_id === 'Truck2' ? 't2' : 't9';
    const hrs = Math.floor(t.minutes/60), mins = t.minutes%60;
    return `<span class="day-truck"><b class="${t_class}">${t.truck_id}</b>: ${t.stops}st · ${Math.round(t.lbs).toLocaleString()}lb · ${hrs}h${String(mins).padStart(2,'0')}</span>`;
  }).join('');
  return `
    <div class="day-block ${isPreview?'preview':''}">
      <div class="day-header">
        <span class="day-label">${escape(d.date_label)}</span>
        <span class="day-total">${totalStops} st · ${Math.round(totalLbs).toLocaleString()} lbs</span>
      </div>
      <div class="day-trucks">${truckLine}</div>
    </div>`;
}

// ── Trucks Available widget ───────────────────────────────────────────────
async function refreshTrucks() {
  try {
    const r = await fetch('/api/truck-availability');
    const data = await r.json();
    const planDate = $('#plan-date').value;
    const commitDays = parseInt($('#commit-days').textContent, 10) || 2;
    const planDateD = planDate ? new Date(planDate) : null;
    const commitCutoff = planDateD ? new Date(planDateD.getTime() + commitDays*86400000) : null;

    const html = ['<div class="trucks-grid">'];
    // Header row
    html.push(`<div></div><div class="strip-label" style="text-align:center">Truck2</div><div class="strip-label" style="text-align:center">Truck9</div>`);
    for (const day of data.days) {
      const d_obj = new Date(day.date);
      const inCommit = planDateD && (d_obj >= planDateD) && (d_obj < commitCutoff);
      const dayClass = inCommit ? 'committed' : 'preview';
      html.push(`<div class="trucks-row-day ${dayClass}">${escape(day.label)}</div>`);
      for (const t of ['Truck2', 'Truck9']) {
        const info = day.trucks[t] || {};
        let cls = '', label = t === 'Truck2' ? 'T2' : 'T9';
        let click = '';
        if (info.reason_sat) {
          cls = 'blocked-sat';
          label = '—';
        } else if (info.available) {
          cls = (t === 'Truck2' ? 't2' : 't9') + ' on';
          label = `✓ ${label}`;
          click = `onclick="toggleTruck('${day.date}','${t}',false)"`;
        } else {
          cls = 'off';
          label = `✗ ${label}`;
          click = `onclick="toggleTruck('${day.date}','${t}',true)"`;
        }
        html.push(`<button class="truck-checkbox ${cls}" ${click}>${label}</button>`);
      }
    }
    html.push('</div>');
    $('#trucks-body').innerHTML = html.join('');
  } catch (e) {
    $('#trucks-body').innerHTML = `<div class="empty">Error: ${escape(String(e))}</div>`;
  }
}

async function toggleTruck(date, truck_id, available) {
  try {
    const r = await fetch('/api/truck-availability', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({date, truck_id, available}),
    });
    const res = await r.json();
    if (!res.ok) { alert('Could not update: ' + (res.error||'')); return; }
    refreshTrucks();
  } catch (e) { alert('Network error: ' + e); }
}

// ── Clients table ─────────────────────────────────────────────────────────
async function refreshClients() {
  try {
    const r = await fetch('/api/clients');
    const data = await r.json();
    STATE.clients = data.clients;
    $('#client-count').textContent = `${data.count} clients`;
    renderClients();
  } catch (e) {
    $('#client-tbody').innerHTML = `<tr><td colspan="10" class="empty">Error: ${escape(String(e))}</td></tr>`;
  }
}

function renderClients() {
  const search = $('#client-search').value.trim().toLowerCase();
  const sort   = $('#client-sort').value;
  const filter = $('#client-filter').value;

  let rows = STATE.clients.slice();
  if (filter === 'urgent')    rows = rows.filter(r => r.dte != null && r.dte < 5);
  if (filter === 'anova')     rows = rows.filter(r => r.has_anova);
  if (filter === 'overrides') rows = rows.filter(r => r.pinned || r.forbidden);
  if (search) rows = rows.filter(r =>
    r.name.toLowerCase().includes(search) || r.id.toLowerCase().includes(search)
  );
  if (sort === 'dte') rows.sort((a,b) => (a.dte ?? 999) - (b.dte ?? 999));
  if (sort === 'name') rows.sort((a,b) => a.name.localeCompare(b.name));
  if (sort === 'id')   rows.sort((a,b) => a.id.localeCompare(b.id));

  const planDate = $('#plan-date').value || todayISO();
  // Update column header with current date — so it's clear what action applies to.
  const dateLabel = planDate
    ? new Date(planDate + 'T00:00:00').toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric'})
    : '';
  $('#action-col-date').textContent = dateLabel ? ` (for ${dateLabel})` : '';
  const tbody = $('#client-tbody');
  tbody.innerHTML = rows.map(r => {
    const dteClass = r.dte == null ? 'safe'
                   : r.dte < 2 ? 'urgent'
                   : r.dte < 5 ? 'warning'
                   : r.dte < 10 ? 'normal' : 'safe';
    const dteLabel = r.dte == null ? '—' : r.dte.toFixed(1);
    const pctClass = r.pct_full < 25 ? 'low' : r.pct_full < 60 ? 'med' : '';
    const anovaCls = !r.has_anova ? 'none'
                   : (r.anova_age_h != null && r.anova_age_h > 24) ? 'stale' : 'live';
    const anovaTitle = r.has_anova
      ? (r.anova_age_h != null ? `Anova ${r.anova_age_h.toFixed(1)}h ago` : 'Anova sensor')
      : 'No sensor';

    // Rate is the AVG from Optimizer_Input (operator-curated). Solver uses
    // the same number. Tooltip surfaces Std Dev (variability) when present —
    // higher std = more variable consumption = harder to predict.
    let rateLabel = r.rate_lpd != null ? `${r.rate_lpd}` : '—';
    let rateTitle = `AVG: ${r.rate_lpd ?? '—'} lbs/day (operator-curated)`;
    if (r.rate_std_dev != null && r.rate_std_dev > 0 && r.rate_lpd) {
      const cv = (r.rate_std_dev / r.rate_lpd * 100).toFixed(0);
      rateTitle += `\nStd Dev: ±${r.rate_std_dev} lbs/day (CV ${cv}%)`;
      // Flag high-variability clients (CV > 40%) — their DTE is fuzzier.
      if ((r.rate_std_dev / r.rate_lpd) > 0.4) {
        rateLabel = `${r.rate_lpd} <span style="color:var(--text-faint);font-size:10px">±${Math.round(r.rate_std_dev)}</span>`;
      }
    }

    // DNS badge — visible flag that this client is Do-Not-Schedule.
    // Row is also dimmed so it visually recedes from the actionable clients.
    const dnsBadge = r.dns
      ? `<span class="dns-badge" title="Do Not Schedule — ${escape(r.dns_reason)}\nEdit Client_List col Q to change.">DNS</span>`
      : '';
    const rowClass = r.dns ? 'dns-row' : '';
    return `
      <tr data-cid="${escape(r.id)}" class="${rowClass}">
        <td class="name-id">${escape(r.id)}</td>
        <td class="name">${escape(displayName(r.name))} ${dnsBadge}</td>
        <td class="num">${r.tank_lbs}</td>
        <td class="num">${Math.round(r.current_lbs)}</td>
        <td class="num">
          <span class="pct-bar"><span class="pct-bar-fill ${pctClass}" style="width:${Math.min(100,r.pct_full)}%"></span></span>
          ${r.pct_full}%
        </td>
        <td class="num" title="${escape(rateTitle)}">${rateLabel}</td>
        <td class="num dte-cell ${dteClass}">${dteLabel}</td>
        <td>${r.last_delivery || '—'}</td>
        <td title="${escape(anovaTitle)}"><span class="anova-dot ${anovaCls}"></span></td>
        <td>${overrideButtonsHTML(r, planDate, dateLabel)}</td>
      </tr>`;
  }).join('');
}

function displayName(s) {
  const parts = s.split(' - ');
  return parts.length >= 3 ? parts.slice(2).join(' - ') : s;
}

function overrideButtonsHTML(r, planDate, dateLabel) {
  // Three visible states per client:
  //   default   — show two action buttons
  //   pinned    — show green "Will visit <date>" + X clear
  //   forbidden — show red "Skipping <date>" + X clear
  if (r.pinned) {
    return `
      <div class="override-cell">
        <span class="override-state pinned" title="Forced to visit on ${dateLabel}. Click × to clear.">
          ✓ Will visit ${escape(dateLabel)}
        </span>
        <button class="btn-mini clear"
                onclick="doOverride('${escape(r.id)}','clear','')"
                title="Clear this override">×</button>
      </div>`;
  }
  if (r.forbidden) {
    return `
      <div class="override-cell">
        <span class="override-state skipped" title="Will not be visited during the commit window (typically tomorrow + day-after). Click × to clear.">
          ✗ Skip commit window
        </span>
        <button class="btn-mini clear"
                onclick="doOverride('${escape(r.id)}','clear','')"
                title="Clear this override">×</button>
      </div>`;
  }
  return `
    <div class="override-cell">
      <button class="btn-action go"
              onclick="doOverride('${escape(r.id)}','pin','${planDate}')"
              title="Force the optimizer to visit this client on ${dateLabel}">
        + Visit
      </button>
      <button class="btn-action skip"
              onclick="doOverride('${escape(r.id)}','skip','${planDate}')"
              title="Tell the optimizer NOT to visit this client during the commit window (tomorrow + day-after).">
        ✗ Skip
      </button>
    </div>`;
}

async function doOverride(cid, action, date) {
  try {
    const r = await fetch('/api/overrides', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({client_id: cid, action, date}),
    });
    const res = await r.json();
    if (!res.ok) { alert('Override failed: ' + (res.error||'')); return; }
    await refreshClients();
  } catch (e) { alert('Network error: ' + e); }
}

// ── Run ───────────────────────────────────────────────────────────────────
async function onRunClick() {
  const date = $('#plan-date').value;
  const solveSecs = parseInt($('#solve-secs').value, 10);
  if (!date) { alert('Pick a first-delivery date'); return; }
  setStatus('running', 'Running…');
  $('#run-btn').disabled = true;
  $('#run-log').textContent = '';
  // Reset and show the progress bar
  STATE.solveBudgetSeconds = solveSecs;
  clearTimeout(STATE.progressFadeTimer);
  showProgress(0, `Starting · budget ${solveSecs}s`, false);
  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({date, solve_seconds: solveSecs}),
    });
    const res = await r.json();
    if (!res.ok) {
      // Blocked by data-freshness check? Offer override.
      if (res.blocked) {
        setStatus('error', 'Blocked');
        $('#run-btn').disabled = false;
        const ok = confirm(
          'DATA FRESHNESS WARNING:\n\n' + (res.error || '') +
          '\n\nRunning anyway WILL produce dangerous results if the file is mid-edit.\n\n' +
          'Click OK only if you are sure the file is fully saved and synced.\n' +
          'Click Cancel to fix and retry.'
        );
        if (ok) {
          // re-fire with force=true
          setStatus('running', 'Running…');
          $('#run-btn').disabled = true;
          const r2 = await fetch('/api/run', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({date, solve_seconds: solveSecs, force: true}),
          });
          const res2 = await r2.json();
          if (!res2.ok) {
            setStatus('error', 'Error');
            $('#run-btn').disabled = false;
            alert(res2.error || 'Could not start run');
            return;
          }
          startRunPolling();
        }
        return;
      }
      setStatus('error', 'Error');
      $('#run-btn').disabled = false;
      alert(res.error || 'Could not start run');
      return;
    }
    startRunPolling();
  } catch (e) {
    setStatus('error', 'Error');
    $('#run-btn').disabled = false;
    alert('Network error: ' + e);
  }
}

function startRunPolling() {
  clearInterval(STATE.runPollTimer);
  STATE.runPollTimer = setInterval(pollRun, 900);
}

async function pollRun() {
  try {
    const r = await fetch('/api/run/status');
    const s = await r.json();
    if (s.log_tail && s.log_tail.length) {
      $('#run-log').textContent = s.log_tail.join('\n');
      $('#run-log').scrollTop = 9e9;
    }

    // Compute elapsed / remaining
    const elapsed = s.elapsed_s ?? 0;
    const budget = STATE.solveBudgetSeconds || 120;
    // Solve can briefly exceed budget on final local search step — cap at 99%
    // until the actual 'done' status arrives, so the bar feels honest.
    const ratio = Math.min(0.99, elapsed / budget);

    if (s.elapsed_s != null) {
      const mm = Math.floor(elapsed / 60);
      const ss = Math.round(elapsed % 60);
      const label = (s.status === 'running' ? 'Running' : 'Solved in') + ' ' + mm + 'm ' + ss + 's';
      $('#plan-stamp').textContent = label;
      if (s.status === 'running') {
        const remaining = Math.max(0, budget - elapsed);
        const rmm = Math.floor(remaining / 60);
        const rss = Math.round(remaining % 60);
        showProgress(ratio,
          `${mm}m ${String(ss).padStart(2,'0')}s elapsed · ~${rmm}m ${String(rss).padStart(2,'0')}s left`,
          false);
      }
    }

    if (s.status === 'done') {
      clearInterval(STATE.runPollTimer);
      setStatus('done', '✓ Done');
      $('#run-btn').disabled = false;
      const mm = Math.floor(elapsed / 60);
      const ss = Math.round(elapsed % 60);
      showProgress(1.0, `✓ Solved in ${mm}m ${ss}s`, true);
      // Fade out after 5 seconds
      STATE.progressFadeTimer = setTimeout(hideProgress, 5000);
      await refreshAll();
    } else if (s.status === 'error') {
      clearInterval(STATE.runPollTimer);
      setStatus('error', 'Error — view run log');
      $('#run-btn').disabled = false;
      showProgress(1.0, '✗ Error — see run log', true);
      $('#progress-fill').style.background = 'var(--danger)';
      $('#progress-label').style.color = 'var(--danger)';
    }
  } catch (e) { /* keep polling */ }
}

// ── Progress bar helpers ───────────────────────────────────────────────────
function showProgress(ratio, label, done) {
  const bar = $('#progress-bar');
  const fill = $('#progress-fill');
  const lbl = $('#progress-label');
  bar.hidden = false;
  bar.classList.remove('fading');
  fill.style.width = (ratio * 100).toFixed(1) + '%';
  fill.classList.toggle('done', !!done);
  lbl.classList.toggle('done', !!done);
  lbl.textContent = label;
}
function hideProgress() {
  const bar = $('#progress-bar');
  bar.classList.add('fading');
  setTimeout(() => { bar.hidden = true; bar.classList.remove('fading'); }, 500);
}

function setStatus(cls, txt) {
  const el = $('#run-status');
  el.className = 'status-pill status-' + cls;
  el.textContent = txt;
}

// ── Utilities ─────────────────────────────────────────────────────────────
function escape(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function todayISO() { return new Date().toISOString().slice(0,10); }

// ── Run log modal close handlers ──────────────────────────────────────────
function closeRunLog() {
  document.getElementById('run-log-modal').hidden = true;
}
function closeRunLogIfBackdrop(ev) {
  // Only close if user clicked the backdrop itself (not its child .modal)
  if (ev.target.id === 'run-log-modal') closeRunLog();
}
// Escape key closes whichever modal is currently open
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape') return;
  const log = document.getElementById('run-log-modal');
  if (log && !log.hidden) { closeRunLog(); return; }
  const urg = document.getElementById('urgency-modal');
  if (urg && !urg.hidden) { closeUrgencyMatrix(); return; }
});


// ── Urgency Lock Matrix modal ─────────────────────────────────────────────
//
// Side-by-side view: frozen pre-run urgency (left edge color) vs the day
// the solver actually scheduled each client. Lets the operator scan top-
// down to confirm RED rows landed in day 0–1 and nothing urgent slipped.

const URG_STATE = { data: null, filter: 'all' };

async function openUrgencyMatrix() {
  const modal = document.getElementById('urgency-modal');
  modal.hidden = false;
  document.getElementById('urgency-body').innerHTML = `<div class="loading" style="padding:20px;">Loading…</div>`;
  try {
    const r = await fetch('/api/urgency-matrix');
    const data = await r.json();
    if (!data.ok) {
      document.getElementById('urgency-body').innerHTML =
        `<div class="empty" style="padding:20px;">${escape(data.error || 'Unknown error')}</div>`;
      return;
    }
    URG_STATE.data = data;
    URG_STATE.filter = 'all';
    renderUrgencyMatrix();
    // Wire filter buttons (re-bound each open in case modal was rebuilt)
    document.querySelectorAll('.urgency-filter').forEach(b => {
      b.onclick = () => {
        URG_STATE.filter = b.dataset.filter;
        renderUrgencyMatrix();
      };
    });
  } catch (e) {
    document.getElementById('urgency-body').innerHTML =
      `<div class="empty" style="padding:20px;">Error: ${escape(String(e))}</div>`;
  }
}

function closeUrgencyMatrix() {
  document.getElementById('urgency-modal').hidden = true;
}
function closeUrgencyIfBackdrop(ev) {
  if (ev.target.id === 'urgency-modal') closeUrgencyMatrix();
}

function renderUrgencyMatrix() {
  const data = URG_STATE.data;
  if (!data) return;
  const horizon = data.horizon || [];
  const commitDays = data.commit_days || 2;

  // Stamp + reserve % in subheader
  document.getElementById('urgency-stamp').textContent =
    `Plan for ${data.today} · ${horizon.length}-day horizon · reserve threshold ${Math.round((data.reserve_pct||0.1)*100)}% (sheet C2)`;

  // Summary tally
  const counts = data.counts || {};
  const misses = data.urgent_misses || 0;
  const summaryHTML = `
    <span class="urg-tally"><span class="urg-dot red"></span>RED <b>${counts.RED||0}</b></span>
    <span class="urg-tally"><span class="urg-dot yel"></span>YEL <b>${counts.YEL||0}</b></span>
    <span class="urg-tally"><span class="urg-dot grn"></span>GRN <b>${counts.GRN||0}</b></span>
    <span class="urg-tally"><span class="urg-dot gry"></span>GRY <b>${counts.GRY||0}</b></span>
    <span class="urg-misses ${misses === 0 ? 'zero' : ''}">${misses === 0 ? '✓ No urgent misses' : `⚠ ${misses} urgent miss${misses === 1 ? '' : 'es'}`}</span>
    <span class="urg-reserve">Pre-run snapshot — frozen at run time, won't shift as data updates.</span>
  `;
  document.getElementById('urgency-summary').innerHTML = summaryHTML;

  // Update filter button "active" highlight
  document.querySelectorAll('.urgency-filter').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === URG_STATE.filter);
  });

  // Filter rows
  let rows = data.rows || [];
  if (URG_STATE.filter === 'urgent') {
    rows = rows.filter(r => r.urgency_bucket === 'RED' || r.urgency_bucket === 'YEL');
  } else if (URG_STATE.filter === 'misses') {
    rows = rows.filter(r => r.urgent_miss);
  }

  // Build header
  const dayHeaders = horizon.map((h, i) => {
    const cls = h.committed ? 'committed' : 'preview';
    return `<th class="day-col ${cls}" title="${h.date}">${escape(h.label)}</th>`;
  }).join('');

  // Build body
  const bodyHTML = rows.map(r => {
    const sched = r.scheduled;
    const schedDay = sched ? sched.day_index : null;
    const dayCells = horizon.map((h, i) => {
      const cls = h.committed ? 'committed' : 'preview';
      if (sched && schedDay === i) {
        const truck = sched.truck || '';
        const tclass = truck === 'Truck2' ? 't2' : truck === 'Truck9' ? 't9' : 'other';
        const label = truck === 'Truck2' ? 'T2' : truck === 'Truck9' ? 'T9' : '●';
        return `<td class="day-cell ${cls}"><span class="day-pill ${tclass}" title="${escape(truck)} · ${Math.round(sched.refill_lbs)} lbs">${label}</span></td>`;
      }
      return `<td class="day-cell ${cls}"><span class="empty-dot">·</span></td>`;
    }).join('');

    // Note column logic
    let noteHTML;
    if (r.urgent_miss) {
      noteHTML = `<span class="miss">⚠ DEFERRED</span> <span class="info">— ${escape(r.deferred_reason || '')}</span>`;
    } else if (sched === null) {
      // Deferred but not flagged as urgent miss (e.g. DNS, NOT_NEEDED_THIS_HORIZON)
      noteHTML = `<span class="info">— ${escape(r.deferred_reason || 'deferred')}</span>`;
    } else {
      // Scheduled
      const lbs = Math.round(sched.refill_lbs || 0).toLocaleString();
      const onTime = (r.urgency_bucket === 'RED' && schedDay <= 1)
                  || (r.urgency_bucket === 'YEL' && schedDay <= 4)
                  || (r.urgency_bucket === 'GRN')
                  || (r.urgency_bucket === 'GRY');
      const tag = onTime ? '<span class="ok">✓ on-time</span>'
                          : (r.urgency_bucket === 'RED' ? '<span class="miss">⚠ late</span>' : '<span class="info">late-ok</span>');
      noteHTML = `<span class="lbs">${lbs} lbs</span> · ${tag}`;
    }

    const dteTxt = r.dte_to_reserve != null ? r.dte_to_reserve.toFixed(1) : '—';
    return `
      <tr class="bucket-${r.urgency_bucket}">
        <td class="bucket-cell"></td>
        <td class="customer-cell">
          <span class="cust-id">${escape(r.client_id)}</span>${escape(r.customer || '')}
        </td>
        <td class="dte-cell">${dteTxt}</td>
        ${dayCells}
        <td class="note-cell">${noteHTML}</td>
      </tr>`;
  }).join('');

  const tableHTML = `
    <table class="urg-table">
      <thead>
        <tr>
          <th></th>
          <th class="customer-col">Customer</th>
          <th class="dte-col" title="Days until tank hits ${Math.round((data.reserve_pct||0.1)*100)}% reserve (frozen at run time)">DTE</th>
          ${dayHeaders}
          <th class="note-col">Note</th>
        </tr>
      </thead>
      <tbody>${bodyHTML || '<tr><td colspan="20" style="padding:20px;text-align:center;color:var(--text-faint);">No clients match this filter.</td></tr>'}</tbody>
    </table>`;
  document.getElementById('urgency-body').innerHTML = tableHTML;
}


// Expose to inline onclick handlers
window.toggleTruck = toggleTruck;
window.doOverride = doOverride;
window.closeRunLog = closeRunLog;
window.closeRunLogIfBackdrop = closeRunLogIfBackdrop;
window.closeUrgencyMatrix = closeUrgencyMatrix;
window.closeUrgencyIfBackdrop = closeUrgencyIfBackdrop;
