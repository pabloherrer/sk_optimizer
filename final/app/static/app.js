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
  // Solver Tuning — min-fill-pct slider
  const slider = $('#min-fill-pct');
  slider.addEventListener('input', onMinFillSliderInput);
  slider.addEventListener('change', onMinFillSliderCommit);   // fires on mouseup
  refreshAll();
});

async function refreshAll() {
  await Promise.all([
    refreshHealthStrip(),
    refreshLastPlan(),
    refreshClients(),
    refreshTrucks(),
    refreshSolverSettings(),
  ]);
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
  } catch (e) {
    $('#strip-file').textContent = `Error: ${e}`;
  }
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
      return;
    }
    STATE.lastPlanToday = p.today;

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

    // Rate column shows the SOLVER's rate (IQR-filtered, recency-weighted).
    // If the spreadsheet's noisy "Last Per Day Cons" differs significantly,
    // a warning icon (⚠️) is shown with explanation in the tooltip.
    let rateLabel = r.rate_lpd != null ? `${r.rate_lpd}` : '—';
    let rateTitle = `Used by solver (IQR-filtered): ${r.rate_lpd ?? '—'} lbs/day`;
    if (r.rate_spreadsheet != null && r.rate_lpd != null) {
      rateTitle += `\nSpreadsheet 'Last Per Day Cons': ${r.rate_spreadsheet} lbs/day`;
      const ratio = r.rate_spreadsheet / r.rate_lpd;
      if (ratio > 1.5 || ratio < 0.67) {
        rateLabel = `${r.rate_lpd} <span style="color:var(--warn)" title="${escape(rateTitle)}">⚠</span>`;
        rateTitle += '\n⚠ Spreadsheet rate may be skewed by a 1-day delivery gap. Solver uses the filtered value.';
      }
    }

    return `
      <tr data-cid="${escape(r.id)}">
        <td class="name-id">${escape(r.id)}</td>
        <td class="name">${escape(displayName(r.name))}</td>
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
// Escape key closes the modal
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') {
    const m = document.getElementById('run-log-modal');
    if (m && !m.hidden) closeRunLog();
  }
});

// Expose to inline onclick handlers
window.toggleTruck = toggleTruck;
window.doOverride = doOverride;
window.closeRunLog = closeRunLog;
window.closeRunLogIfBackdrop = closeRunLogIfBackdrop;
