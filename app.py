#!/usr/bin/env python3
"""
S&K Route Optimizer — Web App
==============================
Run:  python app.py
Open: http://localhost:5050
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# ── Auto-install Flask if missing ─────────────────────────────────────────────
try:
    from flask import Flask, Response, jsonify, request, send_file
except ImportError:
    print("Installing Flask…")
    # Try without --break-system-packages first (conda envs, older pip)
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask', '--quiet'])
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask',
                               '--quiet', '--break-system-packages'])
    from flask import Flask, Response, jsonify, request, send_file

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / 'output'
INPUT_FILE = Path(os.environ.get('SK_INPUT_FILE',
               str(BASE_DIR / 'data' / 'SK_Delivery_System.xlsx')))

app = Flask(__name__)

_is_running = False
_run_lock   = threading.Lock()


# ── Version (from git) ────────────────────────────────────────────────────────

def _get_version() -> dict:
    """Read git commit info. Falls back gracefully if git is not available."""
    try:
        short = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL, text=True
        ).strip()
        date = subprocess.check_output(
            ['git', 'log', '-1', '--format=%cd', '--date=format:%b %d, %Y'],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL, text=True
        ).strip()
        msg = subprocess.check_output(
            ['git', 'log', '-1', '--format=%s'],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL, text=True
        ).strip()
        return {'hash': short, 'date': date, 'msg': msg[:60]}
    except Exception:
        return {'hash': '—', 'date': '—', 'msg': 'git not available'}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime('%b %d, %Y  %I:%M %p')


def get_data_info() -> dict:
    """Return metadata about the input file — filesystem only, no numpy/pandas."""
    info = {
        'input_path':      str(INPUT_FILE),
        'input_modified':  None,
        'last_delivery':   None,
        'client_count':    None,
        'delivery_count':  None,
    }
    if INPUT_FILE.exists():
        info['input_modified'] = _fmt_dt(INPUT_FILE.stat().st_mtime)
        info['client_count']   = '—'   # filled by background fetch
    return info


def get_data_info_full() -> dict:
    """Richer info via openpyxl — called only from /data-info endpoint (subprocess-safe)."""
    info = get_data_info()
    try:
        from openpyxl import load_workbook
        wb = load_workbook(INPUT_FILE, read_only=True, data_only=True)

        if 'Client_List' in wb.sheetnames:
            ws    = wb['Client_List']
            count = sum(
                1 for row in ws.iter_rows(min_row=4, max_col=1, values_only=True)
                if str(row[0] or '').strip().startswith('C')
                and str(row[0] or '').strip()[1:].isdigit()
            )
            info['client_count'] = count

        if 'Delivery_Log' in wb.sheetnames:
            ws   = wb['Delivery_Log']
            rows = list(ws.iter_rows(values_only=True))
            if rows:
                headers  = [str(c or '').lower() for c in rows[0]]
                date_col = next((i for i, h in enumerate(headers) if 'date' in h), None)
                if date_col is not None:
                    from datetime import datetime, date
                    dates = [r[date_col] for r in rows[1:]
                             if isinstance(r[date_col], (datetime, date))]
                    if dates:
                        last = max(dates)
                        info['last_delivery']  = last.strftime('%b %d, %Y')
                        info['delivery_count'] = len(dates)
        wb.close()
    except Exception as e:
        info['read_error'] = str(e)
    return info


def get_latest_outputs() -> dict:
    """Return filenames of the most recent Excel and map outputs."""
    result = {'excel': None, 'map': None}
    if OUTPUT_DIR.exists():
        excels = sorted(OUTPUT_DIR.glob('*.xlsx'),
                        key=lambda f: f.stat().st_mtime, reverse=True)
        maps   = sorted(OUTPUT_DIR.glob('*.html'),
                        key=lambda f: f.stat().st_mtime, reverse=True)
        if excels:
            result['excel'] = excels[0].name
        if maps:
            result['map'] = maps[0].name
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    info    = get_data_info()
    outputs = get_latest_outputs()
    version = _get_version()
    return (HTML_TEMPLATE
            .replace('__DATA_INFO__', json.dumps(info))
            .replace('__OUTPUTS__',   json.dumps(outputs))
            .replace('__VERSION__',   json.dumps(version)))


@app.route('/data-info')
def data_info_route():
    return jsonify(get_data_info_full())


@app.route('/run', methods=['POST'])
def run_optimizer():
    global _is_running

    with _run_lock:
        if _is_running:
            return jsonify({'error': 'Already running'}), 409
        _is_running = True

    data      = request.json or {}
    start_day = int(data.get('start_day', 0))
    solve_sec = int(data.get('solve_sec', 300))

    def generate():
        global _is_running
        try:
            env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
            cmd = [
                sys.executable, '-u',
                str(BASE_DIR / 'run_unified.py'),
                '--start-day', str(start_day),
                '--solve-sec', str(solve_sec),
                '--input-file', str(INPUT_FILE),
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(BASE_DIR),
                env=env,
            )
            for line in proc.stdout:
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            proc.wait()

            outputs = get_latest_outputs()
            result = {
                'done':    True,
                'success': proc.returncode == 0,
                'excel':   outputs['excel'],
                'map':     outputs['map'],
            }
            yield f"data: {json.dumps(result)}\n\n"
        finally:
            _is_running = False

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/download/<path:filename>')
def download(filename):
    filepath = (OUTPUT_DIR / filename).resolve()
    if not str(filepath).startswith(str(OUTPUT_DIR.resolve())):
        return 'Forbidden', 403
    if not filepath.exists():
        return 'File not found', 404
    return send_file(filepath, as_attachment=True)


@app.route('/view/<path:filename>')
def view_map(filename):
    filepath = (OUTPUT_DIR / filename).resolve()
    if not str(filepath).startswith(str(OUTPUT_DIR.resolve())):
        return 'Forbidden', 403
    if not filepath.exists():
        return 'File not found', 404
    return send_file(filepath)


@app.route('/status')
def status():
    outputs = get_latest_outputs()
    return jsonify({'running': _is_running, **outputs})


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>S&amp;K Route Optimizer</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f4f8;
    color: #2c3e50;
    min-height: 100vh;
  }

  /* ── Header ── */
  .header {
    background: linear-gradient(135deg, #1a6faf 0%, #155f9a 100%);
    color: white;
    padding: 18px 36px;
    display: flex;
    align-items: center;
    gap: 14px;
    box-shadow: 0 2px 12px rgba(26,111,175,0.25);
  }
  .header-icon { font-size: 30px; line-height: 1; }
  .header-text h1 { font-size: 19px; font-weight: 700; letter-spacing: -0.3px; }
  .header-text p  { font-size: 12.5px; opacity: 0.75; margin-top: 2px; }
  .header-date {
    margin-left: auto;
    font-size: 12.5px;
    opacity: 0.8;
    text-align: right;
    line-height: 1.5;
  }
  .version-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.2);
    border-radius: 20px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
    color: rgba(255,255,255,0.85);
    margin-top: 5px;
    cursor: default;
  }
  .version-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: #4ade80;
  }

  /* ── Layout ── */
  .main {
    max-width: 720px;
    margin: 30px auto;
    padding: 0 20px 60px;
  }

  /* ── Cards ── */
  .card {
    background: white;
    border-radius: 14px;
    padding: 26px 28px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.07), 0 4px 16px rgba(0,0,0,0.04);
    margin-bottom: 18px;
  }
  .card-title {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #1a6faf;
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    gap: 7px;
  }

  /* ── Data info strip ── */
  .info-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
  }
  .info-item {
    background: #f7fafc;
    border: 1px solid #e8edf2;
    border-radius: 10px;
    padding: 14px 16px;
  }
  .info-item-label {
    font-size: 11px;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 5px;
  }
  .info-item-value {
    font-size: 13.5px;
    font-weight: 600;
    color: #2c3e50;
    line-height: 1.35;
  }
  .info-item-sub {
    font-size: 11.5px;
    color: #94a3b8;
    margin-top: 3px;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
  }
  .badge-green  { background: #d1fae5; color: #065f46; }
  .badge-yellow { background: #fef3c7; color: #92400e; }
  .badge-gray   { background: #f1f5f9; color: #64748b; }

  /* ── Form ── */
  .form-label {
    font-size: 12px;
    font-weight: 600;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 10px;
    display: block;
  }
  .form-group { margin-bottom: 22px; }

  /* Day pills */
  .day-pills { display: flex; gap: 8px; flex-wrap: wrap; }
  .day-pill {
    padding: 9px 18px;
    border: 2px solid #e2e8f0;
    border-radius: 10px;
    background: white;
    color: #64748b;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
    user-select: none;
    text-align: center;
    line-height: 1.2;
  }
  .day-pill:hover:not(.past)  { border-color: #1a6faf; color: #1a6faf; }
  .day-pill.active { background: #1a6faf; border-color: #1a6faf; color: white; }
  .day-pill.past   { opacity: 0.38; cursor: not-allowed; }
  .day-pill.today  { border-color: #94a3b8; }
  .day-pill-name   { font-size: 13px; font-weight: 700; }
  .day-pill-date   { font-size: 11px; font-weight: 500; margin-top: 2px; opacity: 0.75; }
  .day-pill.active .day-pill-date { opacity: 0.85; }

  /* Speed tiles */
  .speed-tiles { display: flex; gap: 10px; }
  .speed-tile {
    flex: 1;
    padding: 13px 10px;
    border: 2px solid #e2e8f0;
    border-radius: 10px;
    cursor: pointer;
    text-align: center;
    transition: all 0.15s ease;
    user-select: none;
  }
  .speed-tile:hover  { border-color: #93c5fd; }
  .speed-tile.active { border-color: #1a6faf; background: #eff6ff; }
  .speed-tile-name   { font-size: 13.5px; font-weight: 700; color: #2c3e50; }
  .speed-tile-time   { font-size: 12px; color: #94a3b8; margin-top: 3px; }

  /* ── Run button ── */
  .btn-run {
    width: 100%;
    padding: 15px;
    background: linear-gradient(135deg, #e67e22 0%, #d35400 100%);
    color: white;
    border: none;
    border-radius: 10px;
    font-size: 15.5px;
    font-weight: 700;
    cursor: pointer;
    transition: opacity 0.2s, transform 0.1s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    margin-top: 4px;
    letter-spacing: 0.2px;
    box-shadow: 0 4px 14px rgba(230,126,34,0.3);
  }
  .btn-run:hover:not(:disabled) { opacity: 0.93; transform: translateY(-1px); }
  .btn-run:active:not(:disabled) { transform: translateY(0); }
  .btn-run:disabled { background: #cbd5e1; box-shadow: none; cursor: not-allowed; }

  /* ── Log ── */
  .status-row {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-bottom: 10px;
  }
  .dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    background: #cbd5e1;
    flex-shrink: 0;
  }
  .dot.running { background: #e67e22; animation: blink 1.1s ease infinite; }
  .dot.ok      { background: #22c55e; }
  .dot.fail    { background: #ef4444; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.35} }
  .status-label { font-size: 13px; font-weight: 600; color: #475569; flex: 1; }
  .status-timer { font-size: 12.5px; font-weight: 600; color: #94a3b8; font-variant-numeric: tabular-nums; }

  /* Progress bar */
  .progress-wrap {
    background: #f1f5f9;
    border-radius: 99px;
    height: 6px;
    margin-bottom: 14px;
    overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    border-radius: 99px;
    background: linear-gradient(90deg, #1a6faf, #e67e22);
    width: 0%;
    transition: width 1s linear;
  }
  .progress-bar.done { background: #22c55e; }

  .log-box {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 9px;
    padding: 14px 16px;
    font-family: 'SF Mono', 'Fira Code', 'Menlo', monospace;
    font-size: 12px;
    line-height: 1.75;
    height: 260px;
    overflow-y: auto;
    color: #334155;
  }
  .ll        { }
  .ll.ok     { color: #16a34a; font-weight: 700; }
  .ll.err    { color: #dc2626; }
  .ll.head   { color: #1a6faf; font-weight: 600; }
  .ll.warn   { color: #d97706; }

  /* ── Results ── */
  .dl-row { display: flex; gap: 12px; }
  .dl-btn {
    flex: 1;
    padding: 14px 18px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    text-decoration: none;
    text-align: center;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    transition: all 0.15s;
  }
  .dl-excel {
    background: linear-gradient(135deg, #16a34a, #15803d);
    color: white;
    border: none;
    box-shadow: 0 3px 10px rgba(22,163,74,0.25);
  }
  .dl-excel:hover { opacity: 0.9; }
  .dl-map {
    background: white;
    color: #1a6faf;
    border: 2px solid #1a6faf;
  }
  .dl-map:hover { background: #eff6ff; }

  /* ── Previous outputs ── */
  .prev-row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: #64748b;
  }
  .prev-link {
    color: #1a6faf;
    text-decoration: none;
    font-weight: 600;
  }
  .prev-link:hover { text-decoration: underline; }
  .divider { color: #cbd5e1; }

  /* ── Path display ── */
  .path-text {
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11.5px;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 6px 10px;
    color: #475569;
    word-break: break-all;
    margin-top: 6px;
  }

  .hidden { display: none !important; }
</style>
</head>
<body>

<div class="header">
  <div class="header-icon">🛢️</div>
  <div class="header-text">
    <h1>S&amp;K Route Optimizer</h1>
    <p>S&amp;K Oil Sales — Phoenix, AZ</p>
  </div>
  <div style="margin-left:auto;text-align:right;">
    <div class="header-date" id="headerDate"></div>
    <div class="version-badge" id="versionBadge" title="Loading...">
      <div class="version-dot"></div>
      <span id="versionText">v—</span>
    </div>
  </div>
</div>

<div class="main">

  <!-- ── Data Status ── -->
  <div class="card">
    <div class="card-title">📂 Data Status</div>
    <div class="info-grid" id="infoGrid">
      <div class="info-item">
        <div class="info-item-label">Input File</div>
        <div class="info-item-value" id="infoClients">—</div>
        <div class="info-item-sub" id="infoModified">—</div>
      </div>
      <div class="info-item">
        <div class="info-item-label">Last Delivery</div>
        <div class="info-item-value" id="infoLastDelivery">—</div>
        <div class="info-item-sub" id="infoDeliveryCount">—</div>
      </div>
      <div class="info-item">
        <div class="info-item-label">Input Location</div>
        <div class="info-item-value" id="infoInputStatus">—</div>
        <div class="path-text" id="infoPath" style="display:none"></div>
      </div>
    </div>
  </div>

  <!-- ── Plan ── -->
  <div class="card">
    <div class="card-title">⚙️ Plan Week</div>

    <div class="form-group">
      <label class="form-label">Start Day</label>
      <div class="day-pills" id="dayPills">
        <!-- populated by JS with real dates -->
      </div>
    </div>

    <div class="form-group">
      <label class="form-label">Solver Speed</label>
      <div class="speed-tiles">
        <div class="speed-tile" data-sec="60" onclick="selectSpeed(this)">
          <div class="speed-tile-name">Quick</div>
          <div class="speed-tile-time">~1 min</div>
        </div>
        <div class="speed-tile active" data-sec="300" onclick="selectSpeed(this)">
          <div class="speed-tile-name">Standard</div>
          <div class="speed-tile-time">~5 min</div>
        </div>
        <div class="speed-tile" data-sec="900" onclick="selectSpeed(this)">
          <div class="speed-tile-name">Thorough</div>
          <div class="speed-tile-time">~15 min</div>
        </div>
      </div>
    </div>

    <button class="btn-run" id="runBtn" onclick="runOptimizer()">
      <span>▶</span> Generate Routes
    </button>
  </div>

  <!-- ── Progress log ── -->
  <div class="card hidden" id="logCard">
    <div class="card-title">📋 Progress</div>
    <div class="status-row">
      <div class="dot running" id="statusDot"></div>
      <div class="status-label" id="statusLabel">Running optimizer…</div>
      <div class="status-timer" id="statusTimer"></div>
    </div>
    <div class="progress-wrap">
      <div class="progress-bar" id="progressBar"></div>
    </div>
    <div class="log-box" id="logBox"></div>
  </div>

  <!-- ── Results ── -->
  <div class="card hidden" id="resultsCard">
    <div class="card-title">✅ Routes Ready</div>
    <div class="dl-row">
      <a class="dl-btn dl-excel" id="excelLink" href="#" download>
        📊 Download Schedule
      </a>
      <a class="dl-btn dl-map" id="mapLink" href="#" target="_blank">
        🗺️ Open Route Map
      </a>
    </div>
  </div>

  <!-- ── Previous outputs ── -->
  <div class="card hidden" id="prevCard">
    <div class="card-title" style="margin-bottom:12px">📁 Last Run</div>
    <div class="prev-row" id="prevRow"></div>
  </div>

</div>

<script>
  // ── Init data ──────────────────────────────────────────────────────────────
  const DATA_INFO = __DATA_INFO__;
  const OUTPUTS   = __OUTPUTS__;
  const VERSION   = __VERSION__;

  // ── Version badge ───────────────────────────────────────────────────────────
  if (VERSION && VERSION.hash !== '—') {
    const vEl = document.getElementById('versionText');
    const vBadge = document.getElementById('versionBadge');
    vEl.textContent = VERSION.hash + ' · ' + VERSION.date;
    vBadge.title = VERSION.msg || '';
  }

  let selectedDay = 0;
  let selectedSec = 300;

  // ── Header date ────────────────────────────────────────────────────────────
  const now = new Date();
  document.getElementById('headerDate').innerHTML =
    now.toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric', year:'numeric'}) +
    '<br>' + now.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'});

  // ── Build day pills: next 5 delivery days from tomorrow ──────────────────
  // Matches compute_plan_dates() in run_unified.py exactly.
  // Work-week days: Tue=2, Wed=3, Thu=4, Fri=5, Sat=6  (JS getDay() values)
  const WORK_JS_DAYS = new Set([2, 3, 4, 5, 6]); // Tue–Sat
  const SHORT_NAMES  = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const today = new Date(now); today.setHours(0,0,0,0);

  const planDates = [];
  let cursor = new Date(today);
  cursor.setDate(cursor.getDate() + 1); // start from tomorrow
  for (let safety = 0; safety < 30 && planDates.length < 5; safety++) {
    if (WORK_JS_DAYS.has(cursor.getDay())) {
      planDates.push(new Date(cursor));
    }
    cursor.setDate(cursor.getDate() + 1);
  }

  const pillsContainer = document.getElementById('dayPills');

  planDates.forEach((d, i) => {
    const name    = SHORT_NAMES[d.getDay()];
    const dateStr = d.toLocaleDateString('en-US', {month:'short', day:'numeric'});

    const pill = document.createElement('div');
    pill.className = 'day-pill';
    pill.dataset.day = i;
    pill.innerHTML = `<div class="day-pill-name">${name}</div><div class="day-pill-date">${dateStr}</div>`;
    pill.onclick = () => selectDay(pill);
    pillsContainer.appendChild(pill);
  });

  // Auto-select first pill
  const allPills = pillsContainer.querySelectorAll('.day-pill');
  if (allPills.length > 0) {
    allPills[0].classList.add('active');
    selectedDay = 0;
  }

  // ── Populate data info ─────────────────────────────────────────────────────
  function populateDataInfo(d) {
    // Clients / file modified
    if (d.client_count) {
      document.getElementById('infoClients').innerHTML =
        d.client_count + ' clients <span class="badge badge-green">loaded</span>';
    } else {
      document.getElementById('infoClients').innerHTML =
        '<span class="badge badge-yellow">not found</span>';
    }
    document.getElementById('infoModified').textContent =
      d.input_modified ? 'Updated ' + d.input_modified : 'File not found';

    // Last delivery
    if (d.last_delivery) {
      document.getElementById('infoLastDelivery').textContent = d.last_delivery;
      document.getElementById('infoDeliveryCount').textContent =
        (d.delivery_count || '?') + ' deliveries on record';
    } else {
      document.getElementById('infoLastDelivery').textContent = 'No data';
      document.getElementById('infoDeliveryCount').textContent = '—';
    }

    // Path
    document.getElementById('infoInputStatus').innerHTML =
      d.client_count
        ? '<span class="badge badge-green">Connected</span>'
        : '<span class="badge badge-yellow">Missing</span>';
    const pathEl = document.getElementById('infoPath');
    if (d.input_path) {
      pathEl.textContent = d.input_path;
      pathEl.style.display = 'block';
    }
  }
  populateDataInfo(DATA_INFO);

  // Fetch richer data info async so it can't crash the page load
  fetch('/data-info')
    .then(r => r.json())
    .then(d => populateDataInfo(d))
    .catch(() => {});

  // ── Previous outputs ───────────────────────────────────────────────────────
  function showPrevOutputs(outputs) {
    if (!outputs.excel && !outputs.map) return;
    const card = document.getElementById('prevCard');
    const row  = document.getElementById('prevRow');
    card.classList.remove('hidden');
    let html = '';
    if (outputs.excel) {
      html += '<a class="prev-link" href="/download/' + outputs.excel + '">📊 ' + outputs.excel + '</a>';
    }
    if (outputs.excel && outputs.map) {
      html += '<span class="divider">·</span>';
    }
    if (outputs.map) {
      html += '<a class="prev-link" href="/view/' + outputs.map + '" target="_blank">🗺️ ' + outputs.map + '</a>';
    }
    row.innerHTML = html;
  }
  showPrevOutputs(OUTPUTS);

  // ── Day / speed selection ──────────────────────────────────────────────────
  function selectDay(el) {
    document.querySelectorAll('.day-pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    selectedDay = parseInt(el.dataset.day);
  }

  function selectSpeed(el) {
    document.querySelectorAll('.speed-tile').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    selectedSec = parseInt(el.dataset.sec);
  }

  // ── Timer & progress ───────────────────────────────────────────────────────
  let _timerInterval = null;
  let _startTime     = null;

  function startTimer(totalSec) {
    _startTime = Date.now();
    const bar  = document.getElementById('progressBar');
    const tmr  = document.getElementById('statusTimer');
    bar.style.width = '0%';
    bar.classList.remove('done');

    _timerInterval = setInterval(() => {
      const elapsed = (Date.now() - _startTime) / 1000;
      const pct     = Math.min((elapsed / totalSec) * 100, 98);
      bar.style.width = pct + '%';

      const eMin = Math.floor(elapsed / 60);
      const eSec = Math.floor(elapsed % 60).toString().padStart(2,'0');
      const tMin = Math.floor(totalSec / 60);
      const tSec = (totalSec % 60).toString().padStart(2,'0');
      tmr.textContent = `${eMin}:${eSec} / ~${tMin}:${tSec}`;
    }, 1000);
  }

  function stopTimer(success) {
    if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
    const bar = document.getElementById('progressBar');
    const tmr = document.getElementById('statusTimer');
    bar.style.width = '100%';
    if (success) bar.classList.add('done');
    if (_startTime) {
      const elapsed = (Date.now() - _startTime) / 1000;
      const eMin = Math.floor(elapsed / 60);
      const eSec = Math.floor(elapsed % 60).toString().padStart(2,'0');
      tmr.textContent = `Done in ${eMin}:${eSec}`;
    }
  }

  // ── Run optimizer ──────────────────────────────────────────────────────────
  function runOptimizer() {
    const btn = document.getElementById('runBtn');
    btn.disabled = true;
    btn.innerHTML = '<span>⏳</span> Running…';

    document.getElementById('logCard').classList.remove('hidden');
    document.getElementById('resultsCard').classList.add('hidden');
    const logBox = document.getElementById('logBox');
    logBox.innerHTML = '';

    setStatus('running', 'Running optimizer…');
    startTimer(selectedSec);

    fetch('/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start_day: selectedDay, solve_sec: selectedSec}),
    }).then(resp => {
      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let   buf     = '';

      function pump() {
        reader.read().then(({done, value}) => {
          if (done) return;
          buf += decoder.decode(value, {stream: true});
          const parts = buf.split('\\n\\n');
          buf = parts.pop();
          for (const part of parts) {
            if (!part.startsWith('data: ')) continue;
            try {
              const msg = JSON.parse(part.slice(6));
              if (msg.line !== undefined) addLine(msg.line);
              if (msg.done)              handleDone(msg);
            } catch(e) {}
          }
          pump();
        });
      }
      pump();
    }).catch(err => {
      addLine('Network error: ' + err.message);
      setStatus('fail', 'Connection lost');
      stopTimer(false);
      resetBtn();
    });
  }

  function addLine(text) {
    if (!text.trim()) return;
    const logBox = document.getElementById('logBox');
    const d = document.createElement('div');
    d.className = 'll';
    if (/✓|Done|complete/i.test(text))             d.classList.add('ok');
    else if (/ERROR|⛔|failed/i.test(text))         d.classList.add('err');
    else if (/^[=\\[\\d]|^\\/|S&K/.test(text))     d.classList.add('head');
    else if (/warn|⚠/i.test(text))                  d.classList.add('warn');
    d.textContent = text;
    logBox.appendChild(d);
    logBox.scrollTop = logBox.scrollHeight;
  }

  function handleDone(data) {
    stopTimer(data.success);
    if (data.success) {
      setStatus('ok', 'Complete — routes are ready');
      if (data.excel || data.map) {
        const card = document.getElementById('resultsCard');
        card.classList.remove('hidden');
        if (data.excel) {
          document.getElementById('excelLink').href = '/download/' + data.excel;
        }
        if (data.map) {
          document.getElementById('mapLink').href = '/view/' + data.map;
        }
        showPrevOutputs(data);
      }
    } else {
      setStatus('fail', 'Failed — see log for details');
    }
    resetBtn();
  }

  function setStatus(state, text) {
    const dot = document.getElementById('statusDot');
    dot.className = 'dot ' + state;
    document.getElementById('statusLabel').textContent = text;
  }

  function resetBtn() {
    const btn = document.getElementById('runBtn');
    btn.disabled = false;
    btn.innerHTML = '<span>▶</span> Generate Routes';
  }
</script>

</body>
</html>"""


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = 5050

    def _open_browser():
        time.sleep(1.8)
        webbrowser.open(f'http://localhost:{port}')

    threading.Thread(target=_open_browser, daemon=True).start()

    print(f"\n  S&K Route Optimizer\n  → http://localhost:{port}\n")
    app.run(host='localhost', port=port, debug=False, threaded=True)
