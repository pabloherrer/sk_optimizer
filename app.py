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
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask', '--quiet'])
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask',
                               '--quiet', '--break-system-packages'])
    from flask import Flask, Response, jsonify, request, send_file

BASE_DIR     = Path(__file__).parent
OUTPUT_DIR   = BASE_DIR / 'output'
INPUT_FILE   = Path(os.environ.get('SK_INPUT_FILE',
               str(BASE_DIR / 'data' / 'SK_Delivery_System.xlsx')))
SUMMARY_FILE = OUTPUT_DIR / 'last_run_summary.json'

# Urgency thresholds (must match config.py)
CRITICAL_DAYS = 1.5
URGENT_DAYS   = 4.0

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
        'input_path':     str(INPUT_FILE),
        'input_filename': INPUT_FILE.name,
        'input_modified': None,
        'input_mtime_ts': None,
        'last_delivery':  None,
        'client_count':   None,
        'delivery_count': None,
        'stale':          False,
    }
    if INPUT_FILE.exists():
        mtime = INPUT_FILE.stat().st_mtime
        info['input_modified'] = _fmt_dt(mtime)
        info['input_mtime_ts'] = mtime
        info['client_count']   = '—'
        age_days = (time.time() - mtime) / 86400
        info['stale'] = age_days > 2
    return info


def get_data_info_full() -> dict:
    """Richer info via openpyxl — called only from /data-info endpoint."""
    info = get_data_info()
    try:
        from openpyxl import load_workbook
        wb = load_workbook(INPUT_FILE, read_only=True, data_only=True)

        if 'Client_List' in wb.sheetnames:
            ws = wb['Client_List']
            count = sum(
                1 for row in ws.iter_rows(min_row=4, max_col=1, values_only=True)
                if str(row[0] or '').strip().startswith('C')
                and str(row[0] or '').strip()[1:].isdigit()
            )
            info['client_count'] = count

            # Urgency breakdown from Days_Until_Stockout or similar
            all_rows = list(ws.iter_rows(values_only=True))
            if all_rows:
                headers  = [str(c or '').lower() for c in all_rows[0]]
                days_col = next(
                    (i for i, h in enumerate(headers)
                     if ('days' in h and 'stock' in h)
                     or ('days' in h and 'remain' in h)
                     or h in ('days_left', 'days_until_stockout', 'days_remaining')),
                    None
                )
                if days_col is None:
                    days_col = next(
                        (i for i, h in enumerate(headers) if 'days' in h), None
                    )
                if days_col is not None:
                    urg = {'critical': 0, 'urgent': 0, 'normal': 0}
                    for row in all_rows[1:]:
                        v = row[days_col]
                        if isinstance(v, (int, float)):
                            if v <= CRITICAL_DAYS:
                                urg['critical'] += 1
                            elif v <= URGENT_DAYS:
                                urg['urgent'] += 1
                            else:
                                urg['normal'] += 1
                    info['urgency_live'] = urg

        if 'Delivery_Log' in wb.sheetnames:
            ws   = wb['Delivery_Log']
            rows = list(ws.iter_rows(values_only=True))
            if rows:
                headers  = [str(c or '').lower() for c in rows[0]]
                date_col = next((i for i, h in enumerate(headers) if 'date' in h), None)
                if date_col is not None:
                    from datetime import date
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


def get_last_run_summary() -> dict | None:
    """Read saved summary from last optimizer run."""
    if SUMMARY_FILE.exists():
        try:
            return json.loads(SUMMARY_FILE.read_text())
        except Exception:
            pass
    return None


def save_run_summary(excel_file: str | None, map_file: str | None, elapsed_sec: float):
    """
    Parse the output Excel to build a run summary and save it as JSON.
    Falls back gracefully if the file can't be read.
    """
    summary = {
        'timestamp':     datetime.now().isoformat(),
        'elapsed_sec':   int(elapsed_sec),
        'excel_file':    excel_file,
        'map_file':      map_file,
        'total_stops':   0,
        'total_miles':   None,
        'routes_count':  0,
        'deferred_count': 0,
        'urgency':       {'critical': 0, 'urgent': 0, 'normal': 0},
    }

    # Parse output Excel for route stats
    if excel_file:
        excel_path = OUTPUT_DIR / excel_file
        if excel_path.exists():
            try:
                from openpyxl import load_workbook
                wb = load_workbook(excel_path, read_only=True, data_only=True)
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    rows = list(ws.iter_rows(values_only=True))
                    if len(rows) < 2:
                        continue
                    headers   = [str(c or '').lower() for c in rows[0]]
                    data_rows = [r for r in rows[1:] if any(c is not None for c in r)]
                    if not data_rows:
                        continue
                    summary['routes_count'] += 1
                    summary['total_stops']  += len(data_rows)

                    # Try to find and sum miles
                    miles_col = next(
                        (i for i, h in enumerate(headers)
                         if 'mile' in h or ('dist' in h and 'km' not in h)),
                        None
                    )
                    if miles_col is not None:
                        total = sum(
                            r[miles_col] for r in data_rows
                            if isinstance(r[miles_col], (int, float))
                        )
                        if summary['total_miles'] is None:
                            summary['total_miles'] = 0
                        summary['total_miles'] += total

                    # Try to find deferred clients
                    defer_col = next(
                        (i for i, h in enumerate(headers) if 'defer' in h or 'skip' in h),
                        None
                    )
                    if defer_col is not None:
                        summary['deferred_count'] += sum(
                            1 for r in data_rows if r[defer_col]
                        )
                wb.close()
                if summary['total_miles'] is not None:
                    summary['total_miles'] = round(summary['total_miles'], 1)
            except Exception:
                pass

    # Urgency snapshot from input file
    try:
        from openpyxl import load_workbook
        wb = load_workbook(INPUT_FILE, read_only=True, data_only=True)
        if 'Client_List' in wb.sheetnames:
            ws       = wb['Client_List']
            all_rows = list(ws.iter_rows(values_only=True))
            if all_rows:
                headers  = [str(c or '').lower() for c in all_rows[0]]
                days_col = next(
                    (i for i, h in enumerate(headers)
                     if ('days' in h and 'stock' in h)
                     or ('days' in h and 'remain' in h)
                     or h in ('days_left', 'days_until_stockout', 'days_remaining')),
                    None
                )
                if days_col is None:
                    days_col = next(
                        (i for i, h in enumerate(headers) if 'days' in h), None
                    )
                if days_col is not None:
                    for row in all_rows[1:]:
                        v = row[days_col]
                        if isinstance(v, (int, float)):
                            if v <= CRITICAL_DAYS:
                                summary['urgency']['critical'] += 1
                            elif v <= URGENT_DAYS:
                                summary['urgency']['urgent'] += 1
                            else:
                                summary['urgency']['normal'] += 1
        wb.close()
    except Exception:
        pass

    OUTPUT_DIR.mkdir(exist_ok=True)
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    info    = get_data_info()
    outputs = get_latest_outputs()
    version = _get_version()
    summary = get_last_run_summary()
    return (HTML_TEMPLATE
            .replace('__DATA_INFO__', json.dumps(info))
            .replace('__OUTPUTS__',   json.dumps(outputs))
            .replace('__VERSION__',   json.dumps(version))
            .replace('__SUMMARY__',   json.dumps(summary)))


@app.route('/data-info')
def data_info_route():
    return jsonify(get_data_info_full())


@app.route('/last-run')
def last_run_route():
    return jsonify(get_last_run_summary() or {})


@app.route('/run', methods=['POST'])
def run_optimizer():
    global _is_running

    with _run_lock:
        if _is_running:
            return jsonify({'error': 'Already running'}), 409
        _is_running = True

    data      = request.json or {}
    solve_sec = int(data.get('solve_sec', 300))
    start_time = time.time()

    def generate():
        global _is_running
        try:
            env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
            cmd = [
                sys.executable, '-u',
                str(BASE_DIR / 'run_unified.py'),
                '--start-day', '0',
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

            outputs      = get_latest_outputs()
            elapsed_sec  = time.time() - start_time
            success      = proc.returncode == 0

            if success:
                save_run_summary(outputs['excel'], outputs['map'], elapsed_sec)

            result = {
                'done':        True,
                'success':     success,
                'excel':       outputs['excel'],
                'map':         outputs['map'],
                'elapsed_sec': int(elapsed_sec),
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

  /* ── Data Status ── */
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
  .info-item.stale {
    border-color: #f59e0b;
    background: #fffbeb;
  }
  .stale-banner {
    display: flex;
    align-items: center;
    gap: 7px;
    background: #fef3c7;
    border: 1px solid #f59e0b;
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 12.5px;
    font-weight: 600;
    color: #92400e;
    margin-bottom: 14px;
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
  .badge-red    { background: #fee2e2; color: #991b1b; }
  .badge-gray   { background: #f1f5f9; color: #64748b; }

  /* ── Plan card ── */
  .plan-window {
    display: flex;
    align-items: center;
    gap: 10px;
    background: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: 10px;
    padding: 13px 16px;
    margin-bottom: 20px;
  }
  .plan-window-icon { font-size: 18px; }
  .plan-window-text {
    font-size: 14px;
    font-weight: 700;
    color: #1e40af;
  }
  .plan-window-sub {
    font-size: 12px;
    color: #3b82f6;
    margin-top: 2px;
  }

  /* Advanced collapsible */
  .advanced-details {
    margin-bottom: 20px;
  }
  .advanced-summary {
    font-size: 12px;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    cursor: pointer;
    user-select: none;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 0;
  }
  .advanced-summary::-webkit-details-marker { display: none; }
  .advanced-summary::before {
    content: '▸';
    font-size: 10px;
    transition: transform 0.15s;
  }
  details[open] .advanced-summary::before { transform: rotate(90deg); }
  .advanced-body { padding-top: 14px; }

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
    letter-spacing: 0.2px;
    box-shadow: 0 4px 14px rgba(230,126,34,0.3);
  }
  .btn-run:hover:not(:disabled)  { opacity: 0.93; transform: translateY(-1px); }
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
  .ll.ok   { color: #16a34a; font-weight: 700; }
  .ll.err  { color: #dc2626; }
  .ll.head { color: #1a6faf; font-weight: 600; }
  .ll.warn { color: #d97706; }

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

  /* ── Last Run card ── */
  .last-run-meta {
    font-size: 12px;
    color: #94a3b8;
    margin-bottom: 14px;
  }
  .stat-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 16px;
  }
  .stat-chip {
    background: #f7fafc;
    border: 1px solid #e8edf2;
    border-radius: 10px;
    padding: 10px 14px;
    text-align: center;
    min-width: 80px;
  }
  .stat-chip-val {
    font-size: 20px;
    font-weight: 800;
    color: #1a6faf;
    line-height: 1;
  }
  .stat-chip-label {
    font-size: 11px;
    color: #94a3b8;
    margin-top: 4px;
    font-weight: 600;
  }

  .urgency-row {
    display: flex;
    gap: 10px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .urgency-pill {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 13px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 700;
  }
  .urgency-pill.critical { background: #fee2e2; color: #991b1b; }
  .urgency-pill.urgent   { background: #fef3c7; color: #92400e; }
  .urgency-pill.normal   { background: #d1fae5; color: #065f46; }
  .urgency-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
  }
  .urgency-pill.critical .urgency-dot { background: #dc2626; }
  .urgency-pill.urgent   .urgency-dot { background: #f59e0b; }
  .urgency-pill.normal   .urgency-dot { background: #22c55e; }

  .last-run-links {
    display: flex;
    gap: 10px;
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid #f1f5f9;
    flex-wrap: wrap;
  }
  .prev-link {
    color: #1a6faf;
    text-decoration: none;
    font-weight: 600;
    font-size: 13px;
  }
  .prev-link:hover { text-decoration: underline; }
  .divider { color: #cbd5e1; font-size: 13px; }

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
    <div id="staleBanner" class="stale-banner hidden">
      ⚠️ Data file hasn't been updated in over 2 days — routes may not reflect current inventory.
    </div>
    <div class="info-grid">
      <div class="info-item" id="fileCell">
        <div class="info-item-label">Input File</div>
        <div class="info-item-value" id="infoClients">—</div>
        <div class="info-item-sub" id="infoFilename">—</div>
      </div>
      <div class="info-item">
        <div class="info-item-label">Last Delivery</div>
        <div class="info-item-value" id="infoLastDelivery">—</div>
        <div class="info-item-sub" id="infoDeliveryCount">—</div>
      </div>
      <div class="info-item">
        <div class="info-item-label">File Updated</div>
        <div class="info-item-value" id="infoModified">—</div>
        <div class="info-item-sub" id="infoStatus">—</div>
      </div>
    </div>
  </div>

  <!-- ── Plan Week ── -->
  <div class="card">
    <div class="card-title">⚙️ Plan Week</div>

    <div class="plan-window">
      <div class="plan-window-icon">📅</div>
      <div>
        <div class="plan-window-text" id="planWindowText">Loading…</div>
        <div class="plan-window-sub">Full 5-day delivery schedule · 4 trucks</div>
      </div>
    </div>

    <details class="advanced-details">
      <summary class="advanced-summary">Advanced Options</summary>
      <div class="advanced-body">
        <div style="font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;">Solver Speed</div>
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
    </details>

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

  <!-- ── Last Run ── -->
  <div class="card hidden" id="lastRunCard">
    <div class="card-title">📁 Last Run</div>
    <div class="last-run-meta" id="lastRunMeta"></div>

    <!-- Urgency snapshot -->
    <div class="urgency-row" id="urgencyRow"></div>

    <!-- Stats chips -->
    <div class="stat-chips" id="statChips"></div>

    <!-- File links -->
    <div class="last-run-links" id="lastRunLinks"></div>
  </div>

</div>

<script>
  // ── Init data ──────────────────────────────────────────────────────────────
  const DATA_INFO = __DATA_INFO__;
  const OUTPUTS   = __OUTPUTS__;
  const VERSION   = __VERSION__;
  const SUMMARY   = __SUMMARY__;

  let selectedSec = 300;

  // ── Version badge ───────────────────────────────────────────────────────────
  if (VERSION && VERSION.hash !== '—') {
    document.getElementById('versionText').textContent = VERSION.hash + ' · ' + VERSION.date;
    document.getElementById('versionBadge').title = VERSION.msg || '';
  }

  // ── Header date ────────────────────────────────────────────────────────────
  const now = new Date();
  document.getElementById('headerDate').innerHTML =
    now.toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric', year:'numeric'}) +
    '<br>' + now.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'});

  // ── Compute planning window ────────────────────────────────────────────────
  // Work-week: Tue–Sat (JS day numbers 2–6)
  const WORK_JS_DAYS = new Set([2, 3, 4, 5, 6]);
  const DAY_NAMES    = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const today = new Date(now); today.setHours(0,0,0,0);

  const planDates = [];
  const cursor = new Date(today);
  cursor.setDate(cursor.getDate() + 1); // start from tomorrow
  for (let safety = 0; safety < 30 && planDates.length < 5; safety++) {
    if (WORK_JS_DAYS.has(cursor.getDay())) planDates.push(new Date(cursor));
    cursor.setDate(cursor.getDate() + 1);
  }

  if (planDates.length >= 2) {
    const fmt = d => d.toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric'});
    document.getElementById('planWindowText').textContent =
      fmt(planDates[0]) + '  →  ' + fmt(planDates[planDates.length - 1]);
  }

  // ── Populate data info ─────────────────────────────────────────────────────
  function populateDataInfo(d) {
    // Stale warning
    if (d.stale) {
      document.getElementById('staleBanner').classList.remove('hidden');
      document.getElementById('fileCell').classList.add('stale');
    }

    // Client count / file status
    if (d.client_count && d.client_count !== '—') {
      document.getElementById('infoClients').innerHTML =
        d.client_count + ' clients <span class="badge badge-green">loaded</span>';
    } else if (d.input_modified) {
      document.getElementById('infoClients').innerHTML =
        '<span class="badge badge-green">Connected</span>';
    } else {
      document.getElementById('infoClients').innerHTML =
        '<span class="badge badge-yellow">Not found</span>';
    }
    // Show filename (not full path)
    document.getElementById('infoFilename').textContent =
      d.input_filename || d.input_path || '—';

    // Last delivery
    if (d.last_delivery) {
      document.getElementById('infoLastDelivery').textContent = d.last_delivery;
      document.getElementById('infoDeliveryCount').textContent =
        (d.delivery_count || '?') + ' deliveries on record';
    } else {
      document.getElementById('infoLastDelivery').textContent = 'No data';
      document.getElementById('infoDeliveryCount').textContent = '—';
    }

    // File modified date
    if (d.input_modified) {
      document.getElementById('infoModified').textContent = d.input_modified;
      document.getElementById('infoStatus').textContent =
        d.stale ? '⚠️ Over 2 days old' : 'Up to date';
    } else {
      document.getElementById('infoModified').textContent = 'Not found';
      document.getElementById('infoStatus').textContent = '—';
    }
  }
  populateDataInfo(DATA_INFO);

  // Fetch richer data async
  fetch('/data-info')
    .then(r => r.json())
    .then(d => populateDataInfo(d))
    .catch(() => {});

  // ── Last Run card ──────────────────────────────────────────────────────────
  function renderLastRun(s) {
    if (!s || Object.keys(s).length === 0) return;
    const card = document.getElementById('lastRunCard');
    card.classList.remove('hidden');

    // Meta line: timestamp + elapsed
    const ts = s.timestamp ? new Date(s.timestamp) : null;
    const tsStr = ts
      ? ts.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}) +
        '  ' + ts.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'})
      : '';
    const elapsed = s.elapsed_sec
      ? Math.floor(s.elapsed_sec / 60) + ':' + String(s.elapsed_sec % 60).padStart(2,'0') + ' solve time'
      : '';
    document.getElementById('lastRunMeta').textContent =
      [tsStr, elapsed].filter(Boolean).join('  ·  ');

    // Urgency pills
    const urg = s.urgency || {};
    let urgHtml = '';
    if (urg.critical) urgHtml += `<div class="urgency-pill critical"><div class="urgency-dot"></div>${urg.critical} critical</div>`;
    if (urg.urgent)   urgHtml += `<div class="urgency-pill urgent"><div class="urgency-dot"></div>${urg.urgent} urgent</div>`;
    if (urg.normal)   urgHtml += `<div class="urgency-pill normal"><div class="urgency-dot"></div>${urg.normal} on track</div>`;
    document.getElementById('urgencyRow').innerHTML = urgHtml || '<span style="color:#94a3b8;font-size:13px;">No urgency data</span>';

    // Stat chips
    const chips = [];
    if (s.routes_count)  chips.push({val: s.routes_count,  label: 'Routes'});
    if (s.total_stops)   chips.push({val: s.total_stops,   label: 'Stops'});
    if (s.total_miles != null) chips.push({val: Math.round(s.total_miles) + ' mi', label: 'Est. Miles'});
    if (s.deferred_count) chips.push({val: s.deferred_count, label: 'Deferred'});
    document.getElementById('statChips').innerHTML = chips.map(c =>
      `<div class="stat-chip"><div class="stat-chip-val">${c.val}</div><div class="stat-chip-label">${c.label}</div></div>`
    ).join('');

    // File links
    let linksHtml = '';
    if (s.excel_file) linksHtml += `<a class="prev-link" href="/download/${s.excel_file}">📊 ${s.excel_file}</a>`;
    if (s.excel_file && s.map_file) linksHtml += `<span class="divider">·</span>`;
    if (s.map_file)   linksHtml += `<a class="prev-link" href="/view/${s.map_file}" target="_blank">🗺️ ${s.map_file}</a>`;
    document.getElementById('lastRunLinks').innerHTML = linksHtml;
  }
  renderLastRun(SUMMARY);

  // ── Speed selection ────────────────────────────────────────────────────────
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
      body: JSON.stringify({solve_sec: selectedSec}),
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
    if (/✓|Done|complete/i.test(text))          d.classList.add('ok');
    else if (/ERROR|⛔|failed/i.test(text))      d.classList.add('err');
    else if (/^[=\\[\\d]|^\\/|S&K/.test(text))  d.classList.add('head');
    else if (/warn|⚠/i.test(text))              d.classList.add('warn');
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
        if (data.excel) document.getElementById('excelLink').href = '/download/' + data.excel;
        if (data.map)   document.getElementById('mapLink').href   = '/view/' + data.map;
      }
      // Refresh Last Run card from server
      fetch('/last-run')
        .then(r => r.json())
        .then(s => renderLastRun(s))
        .catch(() => {});
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
