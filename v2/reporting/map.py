"""
v2.reporting.map — interactive route map (HTML, pure Leaflet — no Folium).

Ports the v1 dispatcher-style UI: day-strip filter, truck filter, sticky
route list with metrics, big depot marker, and — most importantly —
OSRM-routed road polylines (not straight lines).

OSRM responses are cached in `data/route_geom_cache.json`. The format is
identical to v1's, so v1's existing 1k+ cached entries hit immediately
on the v2 run as well.

If OSRM is unreachable for a brand-new (truck, day) waypoint set, we
fall back to straight lines AND mark them dashed so the user can see at
a glance that those polylines aren't real roads.
"""
from __future__ import annotations
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests as _requests
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TRUCK_COLORS: Dict[str, str] = {
    'Truck2': '#1a6faf',
    'Truck9': '#c0392b',
}
DEFAULT_DEPOT = (33.5152, -112.1860)   # SK Foods, AZ — fallback

# Shared cache file with v1 (1k+ entries already there).
GEOM_CACHE_FILE = Path(__file__).resolve().parents[2] / 'data' / 'route_geom_cache.json'


# ─────────────────────────────────────────────────────────────────────────────
# Cache + OSRM helpers
# ─────────────────────────────────────────────────────────────────────────────

def _waypoint_cache_key(waypoints: List[Dict]) -> str:
    """5-decimal lat/lon hash; matches v1 so we share the cache."""
    s = ';'.join(f'{w["lat"]:.5f},{w["lon"]:.5f}' for w in waypoints)
    return hashlib.sha1(s.encode('utf-8')).hexdigest()


def _load_geom_cache() -> dict:
    if GEOM_CACHE_FILE.exists():
        try:
            return json.loads(GEOM_CACHE_FILE.read_text(encoding='utf-8'))
        except Exception as exc:
            print(f'    ⚠  polyline cache unreadable ({exc}); starting fresh')
    return {}


def _save_geom_cache(cache: dict) -> None:
    try:
        GEOM_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GEOM_CACHE_FILE.with_suffix('.tmp.json')
        tmp.write_text(json.dumps(cache), encoding='utf-8')
        tmp.replace(GEOM_CACHE_FILE)
    except Exception as exc:
        print(f'    ⚠  could not persist polyline cache: {exc}')


def _osrm_route_line(coords: List[Tuple[float, float]]) -> Optional[List[List[float]]]:
    """coords = [(lat, lon), ...]. Returns [[lat, lon], ...] or None on failure."""
    if not _HAS_REQUESTS or len(coords) < 2:
        return None
    if len(coords) > 98:                       # OSRM max-waypoints guard
        coords = coords[:98]
    cs = ';'.join(f'{lo:.5f},{la:.5f}' for la, lo in coords)
    try:
        r = _requests.get(
            f'https://router.project-osrm.org/route/v1/driving/{cs}'
            f'?overview=full&geometries=geojson',
            timeout=20,
            headers={'User-Agent': 'sk-optimizer-v2/1.0'},
        )
        r.raise_for_status()
        d = r.json()
        if d.get('code') == 'Ok':
            return [[p[1], p[0]] for p in d['routes'][0]['geometry']['coordinates']]
    except Exception as exc:
        print(f'    OSRM fallback (straight lines): {exc}')
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Plan → route-group payload
# ─────────────────────────────────────────────────────────────────────────────

def _minutes_to_hhmm(shift_start_min: int, minute_offset: int) -> str:
    total = shift_start_min + minute_offset
    h = (total // 60) % 24
    m = total % 60
    suffix = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f'{h12}:{m:02d} {suffix}'


def _committed_set(plan) -> set:
    if not plan.horizon_dates:
        return set()
    n = min(plan.commit_days, len(plan.horizon_dates))
    return set(plan.horizon_dates[:n])


def _build_route_groups(
    plan,
    depot_lat: float,
    depot_lon: float,
    use_osrm: bool = True,
) -> Tuple[List[dict], dict]:
    """Build per-(truck, day) groups + stats. Returns (groups, stats)."""
    cache = _load_geom_cache()
    hits = misses = fails = 0

    committed = _committed_set(plan)
    day_index: Dict[date, int] = {d: i for i, d in enumerate(plan.horizon_dates)}
    shift_start = getattr(plan, 'shift_start_min', 360)
    target_min = getattr(plan, 'shift_target_min', 480)

    groups: List[dict] = []
    for (d, truck_id), route in sorted(plan.routes.items()):
        if not route.stops:
            continue
        color = TRUCK_COLORS.get(truck_id, '#7F8C8D')
        status = 'COMMITTED' if d in committed else 'TENTATIVE'

        # Waypoints: depot → stops → depot (matches v1 format so the cache hits)
        waypoints: List[dict] = [{'lat': depot_lat, 'lon': depot_lon, 'label': 'Depot'}]
        for stop in route.stops:
            # Fill % = fraction of tank this delivery represents (refill/cap).
            # NOT level_after/cap — that would always show ~100% since we
            # fill to capacity. Refill/cap tells the dispatcher how much
            # of the tank was empty when we arrived.
            fill_pct = (stop.delivery_lbs / stop.tank_capacity_lbs * 100
                        if stop.tank_capacity_lbs else 0)
            waypoints.append({
                'lat':       float(stop.lat),
                'lon':       float(stop.lon),
                'stop':      int(stop.sequence),
                'cid':       stop.client_id,
                'label':     str(stop.customer)[:60],
                'address':   stop.address,
                'refill':    int(round(stop.delivery_lbs)),
                'tank_cap':  int(stop.tank_capacity_lbs),
                'tank_before': int(round(stop.level_at_arrival_lbs)),
                'tank_after':  int(round(stop.level_after_lbs)),
                'fill_pct':  round(fill_pct, 1),
                'days_left': round(stop.days_until_stockout_at_arrival, 1),
                'eta':       _minutes_to_hhmm(shift_start, stop.arrival_min),
                'dist_mi':   round(stop.travel_miles, 1),
                'urgency':   (stop.urgency_tier or 'normal').lower(),
                'notes':     stop.notes or '',
            })
        waypoints.append({'lat': depot_lat, 'lon': depot_lon, 'label': 'Depot'})

        # OSRM polyline (cache → fetch → fallback)
        key = _waypoint_cache_key(waypoints)
        polyline = cache.get(key)
        from_cache = polyline is not None
        if polyline is None and use_osrm:
            coords = [(w['lat'], w['lon']) for w in waypoints]
            polyline = _osrm_route_line(coords)
            if polyline and len(polyline) > len(waypoints):
                cache[key] = polyline
                misses += 1
            else:
                fails += 1
        elif from_cache:
            hits += 1
        if polyline is None:
            polyline = [[w['lat'], w['lon']] for w in waypoints]

        depart = _minutes_to_hhmm(shift_start, route.depart_depot_min)
        ret = _minutes_to_hhmm(shift_start, route.return_depot_min)
        hrs, mins = divmod(route.total_minutes, 60)
        cap_pct = round(route.cap_pct or 0, 1)
        ot = route.overtime_minutes or 0
        day_idx = day_index.get(d, -1)
        groups.append({
            'id':         f'{truck_id}_d{day_idx}',
            'truck':      truck_id,
            'date':       d.isoformat(),
            'dayLabel':   d.strftime('%a'),
            'dayFull':    d.strftime('%a %b %d'),
            'dayIndex':   day_idx,
            'status':     status,
            'color':      color,
            'stops':      len(route.stops),
            'load':       int(round(route.total_load_lbs)),
            'capPct':     cap_pct,
            'dist':       round(route.total_miles, 1),
            'timeMin':    int(route.total_minutes),
            'overtimeMin': int(ot),
            'depart':     depart,
            'return':     ret,
            'compA':      f'{route.compartment_a_product} {int(round(route.compartment_a_lbs)):,} lbs' if route.compartment_a_lbs else '',
            'compB':      f'{route.compartment_b_product} {int(round(route.compartment_b_lbs)):,} lbs' if route.compartment_b_lbs else '',
            'summary':    (f'{len(route.stops)} stops · {int(round(route.total_load_lbs)):,} lbs '
                           f'({cap_pct}%) · {round(route.total_miles, 1)} mi · '
                           f'{hrs}h {mins:02d}m'),
            'waypoints':  waypoints,
            'polyline':   polyline,
            'fromCache':  from_cache,
        })

    if misses:
        _save_geom_cache(cache)
    print(f'    Map polyline cache: {hits} hit / {misses} fetched / {fails} fallback '
          f'({len(cache)} total cached)')

    stats = {
        'total_groups': len(groups),
        'cache_hits':   hits,
        'cache_misses': misses,
        'cache_fails':  fails,
    }
    return groups, stats


# ─────────────────────────────────────────────────────────────────────────────
# HTML render
# ─────────────────────────────────────────────────────────────────────────────

def _render_html(route_groups: List[dict], depot_lat: float, depot_lon: float, plan) -> str:
    routes_js = json.dumps(route_groups, ensure_ascii=False)
    run_id = plan.run_id
    today_str = plan.today.strftime('%A %b %d, %Y')
    commit_days = plan.commit_days

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SK Oil — Route Dispatch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root {{
  --bg: #ffffff; --panel: #f8f9fb; --panel-hover: #eef0f4;
  --accent: #d4542b; --blue: #2563eb; --green: #059669;
  --text: #1e293b; --muted: #64748b; --border: #e2e8f0;
  --t2: #1a6faf; --t9: #c0392b;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       display: flex; height: 100vh; overflow: hidden; background: var(--bg); color: var(--text); }}
#sidebar {{ width: 340px; min-width: 340px; background: var(--bg); display: flex; flex-direction: column;
            overflow: hidden; border-right: 1px solid var(--border); box-shadow: 2px 0 12px rgba(0,0,0,.06); z-index: 1000; }}
#sidebar-header {{ padding: 14px 18px 10px; border-bottom: 1px solid var(--border); }}
#sidebar-header h1 {{ font-size: 11px; font-weight: 800; letter-spacing: 2px; text-transform: uppercase;
                      color: var(--accent); margin-bottom: 2px; }}
#sidebar-header h2 {{ font-size: 17px; color: var(--text); font-weight: 700; }}
#sidebar-header .meta {{ font-size: 10px; color: var(--muted); margin-top: 4px; }}

#view-toggle {{ display: flex; border-bottom: 1px solid var(--border); }}
.view-btn {{ flex: 1; padding: 10px 8px; border: none; background: transparent;
             font-size: 12px; font-weight: 700; color: var(--muted); cursor: pointer;
             border-bottom: 2px solid transparent; transition: all .15s; text-align: center; }}
.view-btn:hover {{ color: var(--text); background: var(--panel); }}
.view-btn.active {{ color: var(--green); border-bottom-color: var(--green); }}
.view-btn.active.tent {{ color: var(--blue); border-bottom-color: var(--blue); }}
.vb-count {{ display: inline-block; background: var(--panel); border-radius: 10px;
              padding: 1px 7px; font-size: 10px; font-weight: 800; margin-left: 4px; }}

#stats-bar {{ display: flex; border-bottom: 1px solid var(--border); }}
.stat-cell {{ flex: 1; text-align: center; padding: 8px 4px; border-right: 1px solid var(--border); }}
.stat-cell:last-child {{ border-right: none; }}
.stat-num {{ font-size: 17px; font-weight: 800; color: var(--text); }}
.stat-label {{ font-size: 8px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-top: 1px; }}

#day-strip {{ display: flex; border-bottom: 1px solid var(--border); overflow-x: auto; }}
#day-strip::-webkit-scrollbar {{ display: none; }}
.ds-day {{ flex: 0 0 auto; min-width: 64px; padding: 8px 4px; text-align: center;
           cursor: pointer; border-bottom: 3px solid transparent; transition: all .15s; position: relative; }}
.ds-day:hover {{ background: var(--panel); }}
.ds-day.active {{ border-bottom-color: var(--green); background: rgba(5,150,105,.04); }}
.ds-day.active.tent {{ border-bottom-color: var(--blue); background: rgba(37,99,235,.03); }}
.ds-day .ds-weekday {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                        letter-spacing: .5px; color: var(--muted); }}
.ds-day.active .ds-weekday {{ color: var(--text); }}
.ds-day .ds-date {{ font-size: 16px; font-weight: 800; color: var(--text); }}
.ds-day .ds-stops {{ font-size: 9px; color: var(--muted); margin-top: 1px; }}
.ds-day.tent .ds-weekday, .ds-day.tent .ds-date {{ opacity: .65; }}

#truck-filter {{ display: flex; gap: 6px; padding: 8px 14px; border-bottom: 1px solid var(--border); }}
.truck-btn {{ flex: 1; padding: 6px 8px; border: 1.5px solid var(--border); border-radius: 8px;
              background: transparent; color: var(--muted); font-size: 12px; font-weight: 700;
              cursor: pointer; text-align: center; display: flex; align-items: center; justify-content: center; gap: 6px; }}
.truck-btn:hover {{ border-color: #94a3b8; color: var(--text); }}
.truck-btn.active[data-truck="Truck2"] {{ background: #eff6ff; border-color: var(--t2); color: var(--t2); }}
.truck-btn.active[data-truck="Truck9"] {{ background: #fef2f2; border-color: var(--t9); color: var(--t9); }}
.truck-btn.active[data-truck="All"]    {{ background: var(--panel); border-color: var(--muted); color: var(--text); }}
.truck-dot {{ width: 8px; height: 8px; border-radius: 50%; }}

#route-list {{ overflow-y: auto; flex: 1; }}
.day-header {{ padding: 8px 14px 4px; font-size: 11px; font-weight: 800; text-transform: uppercase;
               letter-spacing: .5px; color: var(--muted); background: var(--panel);
               border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10;
               display: flex; align-items: center; gap: 8px; }}
.dh-badge {{ font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 4px; }}
.dh-committed {{ background: rgba(5,150,105,.1); color: var(--green); }}
.dh-tentative {{ background: rgba(37,99,235,.08); color: var(--blue); }}

.route-card {{ padding: 10px 14px; cursor: pointer; border-bottom: 1px solid var(--border);
               border-left: 4px solid transparent; opacity: .35; transition: all .15s; }}
.route-card.visible {{ opacity: 1; }}
.route-card.tent.visible {{ opacity: .7; }}
.route-card:hover {{ background: var(--panel-hover); }}
.route-card.focused {{ background: #eff6ff; }}
.rc-row {{ display: flex; align-items: center; gap: 10px; }}
.rc-truck {{ font-size: 12px; font-weight: 800; white-space: nowrap;
             display: flex; align-items: center; gap: 5px; }}
.rc-metrics {{ display: flex; gap: 8px; font-size: 11px; color: var(--muted); flex: 1; flex-wrap: wrap; }}
.rc-ot {{ font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 4px;
          background: rgba(192,57,43,.1); color: var(--t9); }}

#legend-bar {{ display: flex; align-items: center; gap: 10px; padding: 8px 14px;
               border-top: 1px solid var(--border); font-size: 9px; color: var(--muted);
               flex-wrap: wrap; flex-shrink: 0; }}
.leg-item {{ display: flex; align-items: center; gap: 3px; }}
.leg-swatch {{ width: 7px; height: 7px; border-radius: 50%; }}

#map {{ flex: 1; }}

.leaflet-popup-content-wrapper {{
  background: #ffffff !important; border-radius: 10px !important;
  box-shadow: 0 8px 30px rgba(0,0,0,.12) !important; border: 1px solid var(--border) !important;
}}
.leaflet-popup-tip {{ background: #ffffff !important; }}
.sk-popup {{ min-width: 240px; }}
.sk-popup .pop-header {{ display: flex; gap: 8px; margin-bottom: 8px;
                         padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
.sk-popup .pop-num {{ width: 28px; height: 28px; border-radius: 50%; display: flex;
                      align-items: center; justify-content: center; font-size: 12px;
                      font-weight: 800; color: white; flex-shrink: 0; }}
.sk-popup .pop-name {{ font-size: 12px; font-weight: 700; line-height: 1.3; }}
.sk-popup .pop-route {{ font-size: 10px; color: var(--muted); }}
.sk-popup .pop-addr {{ font-size: 10px; color: var(--muted); margin-top: 4px; }}
.sk-popup .pop-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 6px; }}
.sk-popup .pop-metric {{ background: #f1f5f9; border-radius: 6px; padding: 6px 8px; }}
.sk-popup .pm-val {{ font-size: 14px; font-weight: 800; }}
.sk-popup .pm-label {{ font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.3px; }}
.sk-popup .pop-urg {{ display: inline-block; margin-top: 6px; padding: 2px 8px;
                      border-radius: 4px; font-size: 10px; font-weight: 700;
                      text-transform: uppercase; letter-spacing: 0.5px; }}
.sk-popup .pop-notes {{ font-size: 10px; color: var(--text); margin-top: 6px; font-style: italic; }}

.leaflet-tooltip {{ background: #1e293b !important; color: #ffffff !important;
                    border: none !important; border-radius: 6px !important;
                    font-size: 11px !important; font-weight: 600 !important; padding: 4px 8px !important; }}
.leaflet-tooltip-top:before {{ border-top-color: #1e293b !important; }}
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">
    <h1>SK Oil Sales</h1>
    <h2>Route Dispatch</h2>
    <div class="meta">{today_str} · {commit_days}-day commit · run {run_id[:14]}</div>
  </div>
  <div id="view-toggle"></div>
  <div id="stats-bar"></div>
  <div id="day-strip"></div>
  <div id="truck-filter"></div>
  <div id="route-list"></div>
  <div id="legend-bar">
    <span style="font-weight:700;color:var(--text)">Urgency:</span>
    <div class="leg-item"><div class="leg-swatch" style="background:#b03030"></div> Stockout</div>
    <div class="leg-item"><div class="leg-swatch" style="background:#c47a1a"></div> Critical</div>
    <div class="leg-item"><div class="leg-swatch" style="background:#9e8a1e"></div> Urgent</div>
    <div class="leg-item"><div class="leg-swatch" style="background:#2a8a4a"></div> Normal</div>
    <span style="margin-left:8px;font-weight:700;color:var(--text)">Line:</span>
    <div class="leg-item">— OSRM road</div>
    <div class="leg-item">⋯ straight (no OSRM)</div>
  </div>
</div>
<div id="map"></div>

<script>
var ROUTES    = {routes_js};
var DEPOT_LAT = {depot_lat};
var DEPOT_LON = {depot_lon};

var URG = {{
  stockout: {{ color: '#b03030', bg: 'rgba(176,48,48,.12)', label: 'STOCKOUT' }},
  critical: {{ color: '#c47a1a', bg: 'rgba(196,122,26,.12)', label: 'CRITICAL' }},
  urgent:   {{ color: '#9e8a1e', bg: 'rgba(158,138,30,.12)', label: 'URGENT' }},
  normal:   {{ color: '#2a8a4a', bg: 'rgba(42,138,74,.12)',  label: 'NORMAL' }}
}};
var TRUCK_COLORS = {{ 'Truck2': '#1a6faf', 'Truck9': '#c0392b' }};

// Group by day for sidebar
var dayMap = {{}};
ROUTES.forEach(function(g) {{
  if (!dayMap[g.dayIndex]) {{
    dayMap[g.dayIndex] = {{
      idx: g.dayIndex, day: g.dayLabel, date: g.date, full: g.dayFull,
      status: g.status, stops: 0, lbs: 0, routes: []
    }};
  }}
  dayMap[g.dayIndex].stops += g.stops;
  dayMap[g.dayIndex].lbs   += g.load;
  dayMap[g.dayIndex].routes.push(g);
}});
var allDayKeys     = Object.keys(dayMap).map(Number).sort(function(a,b){{return a-b;}});
var committedKeys  = allDayKeys.filter(function(k) {{ return dayMap[k].status === 'COMMITTED'; }});
var tentativeKeys  = allDayKeys.filter(function(k) {{ return dayMap[k].status === 'TENTATIVE'; }});

// Map
var map = L.map('map', {{ center: [DEPOT_LAT, DEPOT_LON], zoom: 11, zoomControl: false }});
L.control.zoom({{ position: 'topright' }}).addTo(map);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19, subdomains: 'abcd'
}}).addTo(map);

L.marker([DEPOT_LAT, DEPOT_LON], {{
  icon: L.divIcon({{
    html: '<div style="width:36px;height:36px;background:#d4542b;border-radius:50%;'
        + 'display:flex;align-items:center;justify-content:center;'
        + 'border:3px solid white;box-shadow:0 0 16px rgba(212,84,43,.35),0 4px 12px rgba(0,0,0,.2);">'
        + '<svg width="16" height="16" viewBox="0 0 24 24" fill="white"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg></div>',
    iconSize: [36, 36], iconAnchor: [18, 18], className: ''
  }}), zIndexOffset: 1000
}}).bindTooltip('SK Depot', {{ direction: 'top', offset: [0, -20] }}).addTo(map);

// State
var layerGroups  = {{}};
var focusedRoute = null;
var viewMode     = 'committed';
var selectedDay  = committedKeys.length > 0 ? committedKeys[0] : (allDayKeys[0] || 0);
var activeTrucks = new Set(['Truck2', 'Truck9']);

// Build per-group layer (polyline + markers)
function addRoadLine(group) {{
  var poly  = group.polyline || group.waypoints.map(function(w) {{ return [w.lat, w.lon]; }});
  var isRoad = poly.length > group.waypoints.length;
  var isTent = group.status === 'TENTATIVE';
  var style = isRoad
    ? {{ color: group.color, weight: isTent ? 3 : 4, opacity: isTent ? 0.5 : 0.85 }}
    : {{ color: group.color, weight: isTent ? 2 : 3, opacity: isTent ? 0.35 : 0.6, dashArray: '8 6' }};
  L.polyline(poly, style).addTo(layerGroups[group.id]);
}}

function buildMarkers(group) {{
  var lg = layerGroups[group.id];
  var stops = group.waypoints.filter(function(w) {{ return w.stop !== undefined; }});
  stops.forEach(function(w) {{
    var u = URG[w.urgency] || URG.normal;
    var sz = (w.urgency === 'critical' || w.urgency === 'stockout') ? 28 : 24;
    var fs = sz === 28 ? 11 : 10;
    var glow = (w.urgency === 'critical' || w.urgency === 'stockout')
      ? 'box-shadow:0 0 6px ' + u.color + '88,0 2px 4px rgba(0,0,0,.18);'
      : 'box-shadow:0 2px 4px rgba(0,0,0,.12);';
    var icon = L.divIcon({{
      html: '<div style="width:'+sz+'px;height:'+sz+'px;background:'+group.color+';'
          + 'border-radius:50%;display:flex;align-items:center;justify-content:center;'
          + 'font-size:'+fs+'px;font-weight:800;color:white;'
          + 'border:2.5px solid '+u.color+';'+glow+'">' + w.stop + '</div>',
      iconSize: [sz, sz], iconAnchor: [sz/2, sz/2], className: ''
    }});

    var statusBadge = group.status === 'COMMITTED'
      ? '<span style="color:#059669;font-weight:700">DISPATCH</span>'
      : '<span style="color:#2563eb;font-weight:700">TENTATIVE</span>';

    var popup = '<div class="sk-popup">'
      + '<div class="pop-header">'
      +   '<div class="pop-num" style="background:'+group.color+'">'+w.stop+'</div>'
      +   '<div><div class="pop-name">'+w.label+'</div>'
      +     '<div class="pop-route">'+group.truck+' \\u00b7 '+group.dayFull+' \\u00b7 '+statusBadge+'</div>'
      +     (w.address ? '<div class="pop-addr">'+w.address+'</div>' : '')
      +   '</div>'
      + '</div>'
      + '<div class="pop-grid">'
      +   '<div class="pop-metric"><div class="pm-val">'+w.refill.toLocaleString()+'</div><div class="pm-label">Refill Lbs</div></div>'
      +   '<div class="pop-metric"><div class="pm-val">'+w.fill_pct+'%</div><div class="pm-label">Fill After</div></div>'
      +   '<div class="pop-metric"><div class="pm-val">'+w.eta+'</div><div class="pm-label">ETA</div></div>'
      +   '<div class="pop-metric"><div class="pm-val">'+w.dist_mi+' mi</div><div class="pm-label">From Prev</div></div>'
      +   '<div class="pop-metric"><div class="pm-val">'+w.tank_cap.toLocaleString()+'</div><div class="pm-label">Tank Cap</div></div>'
      +   '<div class="pop-metric"><div class="pm-val">'+w.days_left+'d</div><div class="pm-label">DTE</div></div>'
      + '</div>'
      + '<div class="pop-urg" style="background:'+u.bg+';color:'+u.color+'">'+u.label+'</div>'
      + (w.notes ? '<div class="pop-notes">' + w.notes + '</div>' : '')
      + '</div>';

    L.marker([w.lat, w.lon], {{ icon: icon, zIndexOffset: (w.urgency === 'critical' || w.urgency === 'stockout') ? 500 : 0 }})
      .bindPopup(popup, {{ maxWidth: 300 }})
      .bindTooltip('#'+w.stop+' '+w.label.substring(0,28), {{ direction: 'top', offset: [0, -sz/2 - 4] }})
      .addTo(lg);
  }});
}}

ROUTES.forEach(function(g) {{
  layerGroups[g.id] = L.layerGroup();
  addRoadLine(g);
  buildMarkers(g);
}});

// View toggle
var cStops = 0, tStops = 0;
ROUTES.forEach(function(g) {{
  if (g.status === 'COMMITTED') cStops += g.stops; else tStops += g.stops;
}});
var vtBar = document.getElementById('view-toggle');
vtBar.innerHTML =
  '<button class="view-btn active" data-view="committed">Dispatch <span class="vb-count">'+cStops+' stops</span></button>'
  + '<button class="view-btn tent" data-view="full">Full Plan <span class="vb-count">'+(cStops+tStops)+' stops</span></button>';
vtBar.querySelectorAll('.view-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    viewMode = btn.dataset.view;
    if (viewMode === 'committed' && tentativeKeys.indexOf(selectedDay) >= 0) {{
      selectedDay = committedKeys[0] || allDayKeys[0];
    }}
    updateAll();
  }});
}});

function renderStats() {{
  var visible = ROUTES.filter(function(g) {{
    if (viewMode === 'committed' && g.status !== 'COMMITTED') return false;
    return activeTrucks.has(g.truck);
  }});
  var s=0, lb=0, mi=0, r=0;
  visible.forEach(function(g) {{ s+=g.stops; lb+=g.load; mi+=g.dist; r++; }});
  document.getElementById('stats-bar').innerHTML =
      '<div class="stat-cell"><div class="stat-num">'+s+'</div><div class="stat-label">Stops</div></div>'
    + '<div class="stat-cell"><div class="stat-num">'+Math.round(lb/1000)+'k</div><div class="stat-label">Lbs</div></div>'
    + '<div class="stat-cell"><div class="stat-num">'+r+'</div><div class="stat-label">Routes</div></div>'
    + '<div class="stat-cell"><div class="stat-num">'+Math.round(mi)+'</div><div class="stat-label">Miles</div></div>';
}}

function renderDayStrip() {{
  var strip = document.getElementById('day-strip');
  strip.innerHTML = '';
  var keys = viewMode === 'committed' ? committedKeys : allDayKeys;
  keys.forEach(function(k) {{
    var d = dayMap[k];
    var isTent = d.status === 'TENTATIVE';
    var dateObj = d.date ? new Date(d.date + 'T12:00:00') : null;
    var el = document.createElement('div');
    el.className = 'ds-day' + (k === selectedDay ? ' active' : '') + (isTent ? ' tent' : '');
    el.innerHTML =
        '<div class="ds-weekday">'+d.day+'</div>'
      + '<div class="ds-date">'+(dateObj ? dateObj.getDate() : k)+'</div>'
      + '<div class="ds-stops">'+d.stops+' stops</div>';
    el.addEventListener('click', function() {{
      selectedDay = k;
      focusedRoute = null;
      updateAll();
      var coords = [];
      d.routes.forEach(function(g) {{
        if (!activeTrucks.has(g.truck)) return;
        g.waypoints.forEach(function(w) {{ if (w.stop !== undefined) coords.push([w.lat, w.lon]); }});
      }});
      if (coords.length > 1) map.fitBounds(coords, {{ padding: [60, 60] }});
    }});
    strip.appendChild(el);
  }});
}}

// Truck filter
var truckBar = document.getElementById('truck-filter');
['All', 'Truck2', 'Truck9'].forEach(function(t) {{
  var btn = document.createElement('button');
  btn.className = 'truck-btn active';
  btn.dataset.truck = t;
  if (t === 'All') {{
    btn.innerHTML = 'Both Trucks';
  }} else {{
    btn.innerHTML = '<span class="truck-dot" style="background:'+TRUCK_COLORS[t]+'"></span> '+t.replace('ruck','');
  }}
  btn.addEventListener('click', function() {{
    if (t === 'All') {{
      activeTrucks = new Set(['Truck2', 'Truck9']);
    }} else if (activeTrucks.size === 2) {{
      activeTrucks = new Set([t]);
    }} else if (activeTrucks.has(t) && activeTrucks.size === 1) {{
      activeTrucks = new Set(['Truck2', 'Truck9']);
    }} else {{
      activeTrucks = new Set([t]);
    }}
    updateAll();
  }});
  truckBar.appendChild(btn);
}});

function renderRouteList() {{
  var list = document.getElementById('route-list');
  list.innerHTML = '';
  var keys = viewMode === 'committed' ? committedKeys : allDayKeys;
  keys.forEach(function(k) {{
    var d = dayMap[k];
    var isTent = d.status === 'TENTATIVE';
    var dateObj = d.date ? new Date(d.date + 'T12:00:00') : null;
    var dateLabel = dateObj
      ? dateObj.toLocaleDateString('en-US', {{weekday:'short', month:'short', day:'numeric'}})
      : d.day;
    var hdr = document.createElement('div');
    hdr.className = 'day-header';
    hdr.innerHTML = dateLabel
      + ' <span class="dh-badge '+(isTent?'dh-tentative':'dh-committed')+'">'
      + (isTent?'Tentative':'Dispatch')+'</span>'
      + '<span style="margin-left:auto;font-weight:600;font-size:10px;color:var(--muted);">'
      + d.stops+' stops · '+Math.round(d.lbs/1000)+'k lbs</span>';
    list.appendChild(hdr);
    d.routes.forEach(function(g) {{
      var card = document.createElement('div');
      card.className = 'route-card' + (isTent ? ' tent' : '');
      card.dataset.id = g.id;
      card.dataset.truck = g.truck;
      card.style.borderLeftColor = g.color;
      var hrs = Math.floor(g.timeMin/60), mins = g.timeMin % 60;
      card.innerHTML =
          '<div class="rc-row">'
        +   '<div class="rc-truck"><span class="truck-dot" style="background:'+g.color+'"></span>'+g.truck.replace('ruck','')+'</div>'
        +   '<div class="rc-metrics">'
        +     '<span>'+g.stops+' stops</span>'
        +     '<span>'+(g.load/1000).toFixed(1)+'k lbs · '+g.capPct+'%</span>'
        +     '<span>'+g.dist+' mi</span>'
        +     '<span>'+hrs+'h'+(mins<10?'0':'')+mins+'</span>'
        +     '<span>dep '+g.depart+'</span>'
        +   '</div>'
        +   (g.overtimeMin>0 ? '<span class="rc-ot">OT '+g.overtimeMin+'m</span>' : '')
        + '</div>';
      card.addEventListener('click', function() {{
        if (focusedRoute === g.id) {{
          focusedRoute = null; updateAll();
        }} else {{
          focusedRoute = g.id; selectedDay = k; updateAll();
          var stops = g.waypoints.filter(function(w){{ return w.stop !== undefined; }});
          var coords = stops.map(function(w){{ return [w.lat, w.lon]; }});
          if (coords.length) map.fitBounds(coords, {{ padding: [60, 60] }});
        }}
      }});
      list.appendChild(card);
    }});
  }});
}}

function updateAll() {{
  vtBar.querySelectorAll('.view-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.view === viewMode);
  }});
  renderStats();
  renderDayStrip();
  renderRouteList();
  document.querySelectorAll('.truck-btn').forEach(function(b) {{
    var t = b.dataset.truck;
    if (t === 'All') b.classList.toggle('active', activeTrucks.size === 2);
    else b.classList.toggle('active', activeTrucks.has(t));
  }});
  ROUTES.forEach(function(g) {{
    var dayVis = g.dayIndex === selectedDay;
    var truckVis = activeTrucks.has(g.truck);
    var modeVis = viewMode === 'full' || g.status === 'COMMITTED';
    var visible = dayVis && truckVis && modeVis;
    if (focusedRoute && focusedRoute !== g.id) visible = false;
    var lg = layerGroups[g.id];
    if (visible) {{ if (!map.hasLayer(lg)) map.addLayer(lg); }}
    else         {{ if (map.hasLayer(lg)) map.removeLayer(lg); }}
    var card = document.querySelector('[data-id="'+g.id+'"]');
    if (card) {{
      card.classList.toggle('visible', dayVis && truckVis);
      card.classList.toggle('focused', focusedRoute === g.id);
    }}
  }});
}}

updateAll();
var initCoords = [];
(dayMap[selectedDay] || {{routes:[]}}).routes.forEach(function(g) {{
  g.waypoints.forEach(function(w) {{ if (w.stop !== undefined) initCoords.push([w.lat, w.lon]); }});
}});
if (initCoords.length > 1) map.fitBounds(initCoords, {{ padding: [60, 60] }});
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def write_route_map(
    plan,
    output_path: Path,
    max_days: Optional[int] = None,    # kept for back-compat; unused (we show all)
    depot_lat: float = DEFAULT_DEPOT[0],
    depot_lon: float = DEFAULT_DEPOT[1],
    shift_start_min: int = 360,        # back-compat; map reads from Plan now
    use_osrm: bool = True,
    problem=None,                      # if given, use problem.depot for accuracy
) -> Path:
    """Render the plan to a Leaflet HTML map at `output_path`."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if problem is not None and getattr(problem, 'depot', None) is not None:
        depot_lat = float(problem.depot.lat)
        depot_lon = float(problem.depot.lon)

    groups, _stats = _build_route_groups(plan, depot_lat, depot_lon, use_osrm=use_osrm)
    html = _render_html(groups, depot_lat, depot_lon, plan)
    output_path.write_text(html, encoding='utf-8')
    return output_path
