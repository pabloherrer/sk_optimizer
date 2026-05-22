"""
final.app.server — dashboard for the FINAL route optimizer.

Run:  python -m final.app
Open: http://localhost:5050

Endpoints
---------
GET  /                       dashboard HTML
GET  /api/health             excel mtime, deliveries, anova coverage, etc.
GET  /api/settings           current input file path
POST /api/settings           change input file (writes local_config.json)
POST /api/pick-file          opens native OS file picker, returns path
GET  /api/clients            client list with SOLVER-CORRECT rates + state
GET  /api/last-plan          last archived plan summary (committed/preview split)
GET  /api/overrides          current dashboard Pins/Forbids
POST /api/overrides          add/clear a Pin or Forbid (or all for a client)
GET  /api/truck-availability list of (date, truck_id) unavailable
POST /api/truck-availability replace the unavailability list
POST /api/run                kick off solver subprocess
GET  /api/run/status         polled by frontend; returns status + log tail
GET  /outputs/<filename>     serve generated plan files
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import openpyxl
import pandas as pd
from flask import Flask, Response, jsonify, request, send_file, render_template

from final.app.overrides_store import load_user_overrides, save_user_overrides
from final.app.availability_store import load_unavailability, save_unavailability
from final.sk_solver_final import estimate_consumption_recency_weighted
from v2.ingest.excel import load_clients, load_deliveries

# ── Paths ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
APP_DIR = HERE
REPO = HERE.parent.parent
DATA_DIR = REPO / 'data'
OUTPUT_DIR = REPO / 'final' / 'output'
USER_OVERRIDES = DATA_DIR / 'user_overrides.json'
TRUCK_UNAVAIL = DATA_DIR / 'truck_unavailable.json'
LOCAL_CONFIG = REPO / 'local_config.json'

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__,
             template_folder=str(APP_DIR / 'templates'),
             static_folder=str(APP_DIR / 'static'))

# ── Run state ────────────────────────────────────────────────────────────────
_run_lock = threading.Lock()
_current_run: Dict[str, object] = {
    'status': 'idle', 'started_at': None, 'finished_at': None,
    'log': [], 'date': None, 'returncode': None, 'elapsed_s': None,
}


# ════════════════════════════════════════════════════════════════════════════
# SETTINGS  (local_config.json — which Excel file we're reading)
# ════════════════════════════════════════════════════════════════════════════

def _read_input_file() -> Optional[Path]:
    if LOCAL_CONFIG.exists():
        try:
            cfg = json.loads(LOCAL_CONFIG.read_text(encoding='utf-8'))
            p = Path(cfg.get('input_file') or '')
            return p if p.exists() else None
        except Exception:
            return None
    return None


def _write_input_file(path: Path) -> None:
    cfg = {}
    if LOCAL_CONFIG.exists():
        try:
            cfg = json.loads(LOCAL_CONFIG.read_text(encoding='utf-8'))
        except Exception:
            cfg = {}
    cfg['input_file'] = str(path)
    LOCAL_CONFIG.write_text(json.dumps(cfg, indent=2), encoding='utf-8')


# ════════════════════════════════════════════════════════════════════════════
# DATA HEALTH
# ════════════════════════════════════════════════════════════════════════════

def _data_health() -> Dict:
    f = _read_input_file()
    if f is None:
        return {'ok': False, 'error': 'No input file selected. Click Browse to pick your SK_Delivery_System.xlsx'}
    stat = f.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime)
    age_hours = (datetime.now() - mtime).total_seconds() / 3600.0

    try:
        wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
    except Exception as e:
        return {'ok': False, 'error': f'Could not open Excel: {e}'}

    out = {
        'ok': True, 'path': str(f),
        'mtime': mtime.isoformat(timespec='seconds'),
        'age_hours': round(age_hours, 1),
        'size_kb': round(stat.st_size / 1024, 0),
    }

    if 'Delivery_Log' in wb.sheetnames:
        ws = wb['Delivery_Log']
        n_deliv = 0
        last_date = None
        for i, row in enumerate(ws.iter_rows(min_row=4, max_col=2, values_only=True)):
            d = row[0] if row else None
            if isinstance(d, datetime):
                n_deliv += 1
                if last_date is None or d > last_date:
                    last_date = d
            if i > 6000:
                break
        out['delivery_count'] = n_deliv
        out['last_delivery'] = last_date.date().isoformat() if last_date else None
        if last_date:
            out['last_delivery_days_ago'] = (datetime.now().date() - last_date.date()).days

    if 'Anova_Live' in wb.sheetnames:
        ws = wb['Anova_Live']
        total = 0; with_reading = 0; max_age = 0.0; min_age = 1e9
        for row in ws.iter_rows(min_row=2, max_col=13, values_only=True):
            cid = row[1] if len(row) > 1 else None
            if cid is None: continue
            total += 1
            lvl = row[4] if len(row) > 4 else None
            age = row[12] if len(row) > 12 else None
            if lvl is not None and lvl != '':
                with_reading += 1
                try:
                    a = float(age) if age else 999
                    max_age = max(max_age, a); min_age = min(min_age, a)
                except Exception: pass
        out['anova_total'] = total
        out['anova_with_reading'] = with_reading
        out['anova_pct'] = round(100 * with_reading / max(total, 1), 0)
        out['anova_oldest_hours'] = round(max_age, 1) if with_reading > 0 else None
        out['anova_freshest_hours'] = round(min_age, 1) if with_reading > 0 else None

    if 'Client_List' in wb.sheetnames:
        ws = wb['Client_List']
        n_clients = 0; n_dns = 0
        for row in ws.iter_rows(min_row=4, max_col=17, values_only=True):
            cid = row[0] if row else None
            if cid is None: continue
            n_clients += 1
            dns_cell = row[16] if len(row) > 16 else None
            if str(dns_cell or '').strip().upper() in ('Y', 'YES', 'TRUE', '1'):
                n_dns += 1
        out['client_count'] = n_clients
        out['client_dns'] = n_dns

    wb.close()
    return out


# ════════════════════════════════════════════════════════════════════════════
# CLIENT LIST — uses SOLVER's recency-weighted rate (IQR-filtered)
# ════════════════════════════════════════════════════════════════════════════

def _client_list() -> List[Dict]:
    """Build a list of clients with SOLVER-CORRECT rates + current state."""
    f = _read_input_file()
    if f is None:
        return []

    # Read solver-correct rates (filters out IQR outliers like the
    # OREGANO-CHANDLER 2-deliveries-1-day-apart 400 lpd glitch).
    try:
        clients = load_clients(f)
        deliveries_df = load_deliveries(f)
        solver_rates = estimate_consumption_recency_weighted(
            deliveries_df=deliveries_df,
            clients=clients,
            today=date.today(),
        )
    except Exception as e:
        print(f"WARN: could not compute solver rates: {e}")
        solver_rates = {}

    # Read Optimizer_Input for the rest of the state (current_lbs, last delivery, etc.)
    try:
        wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
    except Exception:
        return []

    # Build id → Client lookup for tank cap
    by_id = {c.id: c for c in clients}

    rows: List[Dict] = []
    ws = wb['Optimizer_Input']
    for row in ws.iter_rows(min_row=6, max_col=20, values_only=True):
        if not row or row[1] is None:
            continue
        cid = str(row[1])
        spreadsheet_rate = row[7]
        last_deliv = row[9]
        est_cur = row[11] or 0
        anova_lvl = row[18] if len(row) > 18 else None
        anova_age = row[19] if len(row) > 19 else None

        # Solver-correct rate (or None if unavailable)
        solver_rate, _ = solver_rates.get(cid, (float('nan'), float('nan')))
        if solver_rate != solver_rate:  # NaN
            solver_rate = None
        else:
            solver_rate = round(float(solver_rate), 1)

        # Tank from clients tuple (master), fallback to Optimizer_Input
        client = by_id.get(cid)
        tank = float(client.tank_capacity_lbs) if client else float(row[6] or 0)
        name = client.customer if client else (row[2] or '')

        current_lbs = float(est_cur) if est_cur else 0
        pct = round(100 * current_lbs / tank, 0) if tank > 0 else 0

        # DTE using SOLVER rate; fallback to spreadsheet DTE if rate missing
        if solver_rate and solver_rate > 0:
            dte = round(current_lbs / solver_rate, 1)
        else:
            spreadsheet_dte = row[13]
            dte = round(float(spreadsheet_dte), 1) if isinstance(spreadsheet_dte, (int, float)) else None

        rows.append({
            'id': cid,
            'name': str(name),
            'tank_lbs': tank,
            'rate_lpd': solver_rate,                          # solver-correct
            'rate_spreadsheet': round(float(spreadsheet_rate), 1) if spreadsheet_rate else None,
            'current_lbs': round(current_lbs, 1),
            'pct_full': pct,
            'dte': dte,
            'last_delivery': str(last_deliv)[:10] if last_deliv else None,
            'has_anova': anova_lvl is not None,
            'anova_age_h': round(float(anova_age), 1) if isinstance(anova_age, (int, float)) else None,
        })
    wb.close()
    rows.sort(key=lambda r: r['dte'] if r['dte'] is not None else 999)
    return rows


# ════════════════════════════════════════════════════════════════════════════
# LAST PLAN
# ════════════════════════════════════════════════════════════════════════════

def _last_plan() -> Optional[Dict]:
    archive_dir = OUTPUT_DIR / 'archive'
    if not archive_dir.exists():
        return None
    files = sorted(archive_dir.glob('plan_*.json'), key=lambda p: p.stat().st_mtime)
    if not files: return None
    try:
        plan = json.loads(files[-1].read_text(encoding='utf-8'))
    except Exception:
        return None

    today_iso = plan.get('today', '')
    today = date.fromisoformat(today_iso) if today_iso else date.today()
    horizon = [date.fromisoformat(d) for d in plan.get('horizon_dates', [])]
    commit_days = int(plan.get('commit_days', 2))
    committed_dates = set(horizon[:commit_days])

    # The JSON archive nests each route under a 'route' sub-key (the dict
    # was serialized as [{date, truck_id, route: {…full route…}}, ...] to
    # avoid tuple-keyed dicts). Unwrap to a flat shape so we can read
    # total_load_lbs / stops without surprises.
    raw_routes = plan.get('routes', [])
    flat_routes: List[Dict] = []
    if isinstance(raw_routes, list):
        for r in raw_routes:
            route_data = r.get('route', r) if isinstance(r, dict) else {}
            flat_routes.append({
                'date': r.get('date'),
                'truck_id': r.get('truck_id'),
                **route_data,
            })
    elif isinstance(raw_routes, dict):
        for _k, r in raw_routes.items():
            if 'route' in r and isinstance(r['route'], dict):
                flat_routes.append({**r['route'], 'date': r.get('date'), 'truck_id': r.get('truck_id')})
            else:
                flat_routes.append(r)

    by_date: Dict[str, List[Dict]] = {}
    for r in flat_routes:
        by_date.setdefault(str(r.get('date', '')), []).append(r)

    committed: List[Dict] = []
    preview: List[Dict] = []
    for d_iso in sorted(by_date.keys()):
        day_routes = by_date[d_iso]
        d_obj = date.fromisoformat(d_iso) if len(d_iso) == 10 else None
        is_committed = d_obj in committed_dates if d_obj else False
        summary = {
            'date': d_iso,
            'date_label': d_obj.strftime('%a %b %d') if d_obj else d_iso,
            'committed': is_committed,
            'trucks': [
                {
                    'truck_id': r.get('truck_id'),
                    'stops': len(r.get('stops') or []),
                    'lbs': float(r.get('total_load_lbs') or 0),
                    'cap_pct': float(r.get('cap_pct') or 0),
                    'minutes': int(r.get('total_minutes') or 0),
                    'miles': float(r.get('total_miles') or 0),
                }
                for r in day_routes
            ],
        }
        (committed if is_committed else preview).append(summary)

    # List output files that ACTUALLY exist for this plan, so the frontend
    # doesn't construct guess-URLs that 404.
    files = {}
    if OUTPUT_DIR.exists():
        excel = OUTPUT_DIR / f'plan_{today_iso}.xlsx'
        if excel.exists():
            files['excel'] = excel.name
        map_file = OUTPUT_DIR / f'route_map_{today_iso}.html'
        if map_file.exists():
            files['map'] = map_file.name
        # SmartService CSV is named after first delivery date in plan
        # (typically today+1, but find it by glob to be safe).
        csv_candidates = sorted(OUTPUT_DIR.glob('smartservice_*.csv'),
                                 key=lambda p: p.stat().st_mtime)
        if csv_candidates:
            files['csv'] = csv_candidates[-1].name

    return {
        'today': today_iso,
        'horizon_dates': [d.isoformat() for d in horizon],
        'commit_days': commit_days,
        'horizon_days': len(horizon),
        'committed': committed,
        'preview': preview,
        'total_stops': plan.get('total_stops'),
        'total_lbs': plan.get('total_lbs_delivered'),
        'total_miles': plan.get('total_miles'),
        'avg_fill': plan.get('avg_fill_pct'),
        'objective_dollars': plan.get('objective_cost_dollars'),
        'solve_seconds': plan.get('solve_seconds'),
        'generated_at': plan.get('generated_at'),
        'files': files,
    }


# ════════════════════════════════════════════════════════════════════════════
# WORKDAYS — used by truck-availability widget
# ════════════════════════════════════════════════════════════════════════════

def _upcoming_workdays(start: date, count: int = 10) -> List[date]:
    """Tue–Sat working days starting from `start`."""
    _DOW = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    work_set = {'Tue', 'Wed', 'Thu', 'Fri', 'Sat'}
    out: List[date] = []
    cursor = start
    for _ in range(60):
        if len(out) >= count:
            break
        if _DOW[cursor.weekday()] in work_set:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


# ════════════════════════════════════════════════════════════════════════════
# ROUTES — UI
# ════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    default_date = (date.today() + timedelta(days=1)).isoformat()
    return render_template('index.html', default_date=default_date)


# ════════════════════════════════════════════════════════════════════════════
# ROUTES — JSON API
# ════════════════════════════════════════════════════════════════════════════

@app.route('/api/health')
def api_health():
    return jsonify(_data_health())


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'GET':
        return jsonify({'input_file': str(_read_input_file()) if _read_input_file() else None})
    body = request.get_json(force=True) or {}
    new_path = body.get('input_file')
    if not new_path:
        return jsonify({'ok': False, 'error': 'missing input_file'}), 400
    p = Path(new_path)
    if not p.exists() or not p.is_file():
        return jsonify({'ok': False, 'error': f'File not found: {new_path}'}), 400
    if p.suffix.lower() != '.xlsx':
        return jsonify({'ok': False, 'error': 'Pick a .xlsx file'}), 400
    _write_input_file(p)
    return jsonify({'ok': True, 'input_file': str(p)})


@app.route('/api/pick-file', methods=['POST'])
def api_pick_file():
    """Open native OS file picker. Returns selected path."""
    current = _read_input_file()
    initial_dir = str(current.parent) if current else str(Path.home() / 'Downloads')
    sysname = platform.system()
    try:
        if sysname == 'Darwin':
            script = (
                'POSIX path of (choose file '
                'with prompt "Select your SK_Delivery_System.xlsx file" '
                'of type {"xlsx", "org.openxmlformats.spreadsheetml.sheet"} '
                f'default location POSIX file "{initial_dir}")'
            )
            proc = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                p = proc.stdout.strip()
                _write_input_file(Path(p))
                return jsonify({'ok': True, 'path': p})
            if 'cancel' in (proc.stderr or '').lower():
                return jsonify({'ok': False, 'cancelled': True})
            return jsonify({'ok': False, 'error': proc.stderr.strip() or 'Picker failed'})

        elif sysname == 'Windows':
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                '$d = New-Object System.Windows.Forms.OpenFileDialog; '
                '$d.Filter = "Excel files (*.xlsx)|*.xlsx"; '
                f'$d.InitialDirectory = "{initial_dir}"; '
                '$d.Title = "Select your SK_Delivery_System.xlsx file"; '
                'if ($d.ShowDialog() -eq "OK") { Write-Output $d.FileName }'
            )
            proc = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps],
                capture_output=True, text=True, timeout=600,
            )
            path = (proc.stdout or '').strip()
            if path:
                _write_input_file(Path(path))
                return jsonify({'ok': True, 'path': path})
            return jsonify({'ok': False, 'cancelled': True})

        else:
            for cmd in (
                ['zenity', '--file-selection',
                 '--title=Select your SK_Delivery_System.xlsx file',
                 '--file-filter=Excel files | *.xlsx',
                 f'--filename={initial_dir}/'],
                ['kdialog', '--getopenfilename', initial_dir, '*.xlsx'],
            ):
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                    if proc.returncode == 0 and proc.stdout.strip():
                        p = proc.stdout.strip()
                        _write_input_file(Path(p))
                        return jsonify({'ok': True, 'path': p})
                except FileNotFoundError:
                    continue
            return jsonify({'ok': False, 'error': 'No GUI file picker available (install zenity or kdialog)'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/clients')
def api_clients():
    overrides = load_user_overrides(USER_OVERRIDES)
    pin_ids = {p.client_id for p in overrides.pins}
    forbid_ids = {f.client_id for f in overrides.forbids}
    rows = _client_list()
    for r in rows:
        r['pinned'] = r['id'] in pin_ids
        r['forbidden'] = r['id'] in forbid_ids
    return jsonify({'clients': rows, 'count': len(rows)})


@app.route('/api/last-plan')
def api_last_plan():
    plan = _last_plan()
    if plan is None:
        return jsonify({'ok': False, 'error': 'No plan yet — click RUN'})
    plan['ok'] = True
    return jsonify(plan)


@app.route('/api/overrides', methods=['GET'])
def api_overrides_get():
    if not USER_OVERRIDES.exists():
        return jsonify({'pins': [], 'forbids': []})
    try:
        return jsonify(json.loads(USER_OVERRIDES.read_text(encoding='utf-8')))
    except Exception:
        return jsonify({'pins': [], 'forbids': []})


@app.route('/api/overrides', methods=['POST'])
def api_overrides_post():
    body = request.get_json(force=True) or {}
    action = body.get('action')
    cid = str(body.get('client_id', '')).strip()
    d_str = body.get('date')
    if action not in ('pin', 'skip', 'clear') or not cid:
        return jsonify({'ok': False, 'error': 'bad request'}), 400

    current = json.loads(USER_OVERRIDES.read_text(encoding='utf-8')) \
        if USER_OVERRIDES.exists() else {'pins': [], 'forbids': []}
    pins = [p for p in current.get('pins', []) if p.get('client_id') != cid]
    forbids = [f for f in current.get('forbids', []) if f.get('client_id') != cid]

    if action == 'pin' and d_str:
        pins.append({'client_id': cid, 'date': d_str,
                     'reason': 'dashboard pin', 'created_at': datetime.now().isoformat()})
    elif action == 'skip' and d_str:
        forbids.append({'client_id': cid, 'dates': [d_str],
                        'reason': 'dashboard skip', 'created_at': datetime.now().isoformat()})
    save_user_overrides(USER_OVERRIDES, pins, forbids)
    return jsonify({'ok': True, 'pins': pins, 'forbids': forbids})


@app.route('/api/truck-availability', methods=['GET'])
def api_truck_avail_get():
    """Return list of upcoming work-days with each truck's availability status."""
    start = date.today()
    days = _upcoming_workdays(start, count=10)
    unavail = load_unavailability(TRUCK_UNAVAIL)
    trucks = ['Truck2', 'Truck9']  # could read from fleet.yaml later
    rows = []
    _DOW = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    for d in days:
        is_sat = _DOW[d.weekday()] == 'Sat'
        per_truck = {}
        for t in trucks:
            # Saturday rule: Truck9 is automatically unavailable
            sat_blocked = (is_sat and t != 'Truck2')
            user_blocked = ((d, t) in unavail)
            per_truck[t] = {
                'available': not (sat_blocked or user_blocked),
                'reason_sat': sat_blocked,
                'reason_user': user_blocked,
            }
        rows.append({
            'date': d.isoformat(),
            'label': d.strftime('%a %b %d'),
            'is_saturday': is_sat,
            'trucks': per_truck,
        })
    return jsonify({'days': rows, 'trucks': trucks})


@app.route('/api/truck-availability', methods=['POST'])
def api_truck_avail_post():
    """Toggle one (date, truck) entry."""
    body = request.get_json(force=True) or {}
    d_str = body.get('date')
    tid = body.get('truck_id')
    available = bool(body.get('available'))
    if not d_str or not tid:
        return jsonify({'ok': False, 'error': 'missing date or truck_id'}), 400
    try:
        d = date.fromisoformat(d_str)
    except Exception:
        return jsonify({'ok': False, 'error': 'bad date'}), 400

    # Load current
    unavail = load_unavailability(TRUCK_UNAVAIL)
    if available:
        unavail.discard((d, tid))
    else:
        unavail.add((d, tid))
    entries = [
        {'date': d.isoformat(), 'truck_id': t, 'reason': 'dashboard'}
        for (d, t) in sorted(unavail)
    ]
    save_unavailability(TRUCK_UNAVAIL, entries)
    return jsonify({'ok': True, 'unavailable_count': len(entries)})


@app.route('/api/run', methods=['POST'])
def api_run():
    body = request.get_json(force=True) or {}
    plan_date = body.get('date')
    if not plan_date:
        return jsonify({'ok': False, 'error': 'Missing date'}), 400
    try:
        date.fromisoformat(plan_date)
    except Exception:
        return jsonify({'ok': False, 'error': 'Bad date'}), 400

    with _run_lock:
        if _current_run['status'] == 'running':
            return jsonify({'ok': False, 'error': 'A run is already in progress'}), 409
        _current_run.update({
            'status': 'running', 'started_at': time.time(),
            'finished_at': None, 'log': [], 'date': plan_date,
            'returncode': None, 'elapsed_s': None,
        })

    solve_seconds = int(body.get('solve_seconds', 120))
    threading.Thread(
        target=_run_solver_thread,
        args=(plan_date, solve_seconds),
        daemon=True,
    ).start()
    return jsonify({'ok': True})


def _run_solver_thread(plan_date: str, solve_seconds: int):
    cmd = [sys.executable, '-u', '-m', 'final.sk_solver_final',
           '--today', plan_date, '--solve-seconds', str(solve_seconds)]
    env = dict(os.environ)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True,
        )
        for line in proc.stdout:  # type: ignore
            with _run_lock:
                _current_run['log'].append(line.rstrip())
                if len(_current_run['log']) > 500:
                    _current_run['log'] = _current_run['log'][-500:]
        proc.wait()
        with _run_lock:
            _current_run['finished_at'] = time.time()
            _current_run['elapsed_s'] = _current_run['finished_at'] - _current_run['started_at']
            _current_run['returncode'] = proc.returncode
            _current_run['status'] = 'done' if proc.returncode == 0 else 'error'
    except Exception as e:
        with _run_lock:
            _current_run['log'].append(f'ERROR: {e}')
            _current_run['status'] = 'error'
            _current_run['finished_at'] = time.time()
            _current_run['elapsed_s'] = _current_run['finished_at'] - _current_run['started_at']


@app.route('/api/run/status')
def api_run_status():
    with _run_lock:
        snap = dict(_current_run)
        snap['log_tail'] = snap['log'][-80:]
        snap['log_count'] = len(snap['log'])
    elapsed = None
    if snap['started_at']:
        end = snap['finished_at'] or time.time()
        elapsed = round(end - snap['started_at'], 1)
    snap['elapsed_s'] = elapsed
    return jsonify(snap)


@app.route('/outputs/<path:filename>')
def serve_output(filename):
    p = OUTPUT_DIR / filename
    if not p.exists():
        return jsonify({'error': 'not found'}), 404
    # .html (route maps) → render inline in browser.
    # .xlsx and .csv → force download so browser doesn't open a blank tab.
    suffix = p.suffix.lower()
    as_attachment = suffix in ('.xlsx', '.csv', '.json', '.zip')
    return send_file(p, as_attachment=as_attachment, download_name=p.name)


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main(host: str = '127.0.0.1', port: int = 5050):
    print('=' * 70)
    print('  S&K Route Optimizer Dashboard')
    print(f'  Open: http://{host}:{port}')
    print('=' * 70)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
