# S&K Oil Sales — Route Optimizer

A weekly route optimizer for S&K Oil Sales. Reads a single Excel workbook, produces a 5-day truck schedule (Tue–Sat) with maps, and flags problems in plain English before anything runs.

---

## Quick start

```bash
cd sk_optimizer
python run_unified.py
```

Outputs land in `output/`:
- `sk_unified_schedule.xlsx` — one sheet per truck/day + summary + deferred list
- `sk_unified_map.html` — open in any browser; route polylines drawn on Phoenix map

### Useful flags

```bash
python run_unified.py --validate-only            # Check inputs, don't solve
python run_unified.py --today 2026-04-05         # Solve as-of a specific date
python run_unified.py --solve-sec 600            # Give the solver 10 minutes
python run_unified.py --demo                     # Run on the fictional demo dataset
```

### First-time install (Mac)

```bash
# Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12 in its own venv (keeps Anaconda out of the way)
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv ~/sk_venv
source ~/sk_venv/bin/activate

# Dependencies
pip install pandas openpyxl ortools folium requests numpy
```

From then on, just `source ~/sk_venv/bin/activate` before each run.

---

## What lives in the Excel file

Everything routable is in `data/SK_Delivery_System.xlsx`. No more constants buried in code.

| Sheet | Purpose |
|---|---|
| **Client_List** | Master list of restaurants. One row per client. |
| **Delivery_Log** | Historical deliveries. Append-only. Used to estimate consumption rates. |
| **Client_Time_Windows** | Per-client open/close hours by day of week (optional). |
| **Client_Closures** | Date-range closures — vacations, renovations, temp closed (optional). |
| **Trucks** | Fleet config — capacity, pump rate, setup time per truck. |
| **Depot** | Depot GPS, shift start/end, load/unload times, work days. |

### Adding a new client

Add a row to `Client_List` starting at row 4:

| Col | Field | Example | Required? |
|---|---|---|---|
| A | ID | `C158` | yes (prefix with `C`) |
| B | Customer | `Joe's Diner` | yes |
| C | Zone | `3` | recommended |
| D | Zone_Code | `3N` | recommended |
| E | Street | `123 Main St` | recommended |
| F | City | `Phoenix` | recommended |
| G | State | `AZ` | yes |
| H | Latitude | `33.5152` | **required for routing** |
| I | Longitude | `-112.1674` | **required for routing** |
| J | Tank_lbs | `2000` | **required for routing** |
| K | Product | `CANOLA` or `FRYERS CHOICE` | yes |
| L | Service_Min | `25` | optional (overrides default) |
| M | Access_Notes | `gate code 1234` | optional |
| N | Phone | `602-555-0100` | optional |

No GPS or no tank size → client is automatically deferred with a clear reason.

### Adding a time window

One row per (client, day) pair in `Client_Time_Windows`:

| Client_ID | Day_of_Week | Open_HHMM | Close_HHMM | Notes |
|---|---|---|---|---|
| C042 | Tue | 10:00 | 14:00 | No deliveries during lunch rush |
| C042 | Wed | 10:00 | 14:00 | |

If a client has no rows, they're assumed open all shift. Days not listed for a client are treated as "always open" (add an explicit 00:00–00:00 row to block a day entirely — or use the closure sheet).

### Adding a closure

| Client_ID | Start_Date | End_Date | Reason |
|---|---|---|---|
| C042 | 2026-04-20 | 2026-04-27 | Renovations |
| C101 | 2026-07-01 | 2026-07-07 | Owner vacation |

Inclusive on both ends. The solver skips these clients on those days.

---

## Reading the output

### Excel — one sheet per truck/day

Each stop row shows: arrival time, client, product, lbs delivered, compartment used, cumulative miles, urgency color. The **Summary** sheet aggregates the week. The **Deferred** sheet lists every client who didn't make the cut, with a reason code.

### Deferral reason codes

| Code | Meaning | Fix |
|---|---|---|
| `NO_GPS` | Latitude or longitude missing from Client_List | Add GPS to row |
| `NO_TANK_SIZE` | Tank_lbs missing or ≤ 0 | Add tank size |
| `NO_CONSUMPTION_DATA` | Not enough delivery history to estimate rate | Log at least 2 real deliveries, or wait for next week |
| `CLOSED_ALL_WEEK` | Closure covers every day this week | Expected — wait for closure to end |
| `NO_CAPACITY` | Solver chose to defer — trucks were full this week | Client will resurface next week with higher urgency |
| `TIME_WINDOW_INFEASIBLE` | Travel time from depot exceeds client's window | Widen window or adjust shift |
| `SHIFT_OVERFLOW` | No slot fits within shift length | Increase shift minutes in Depot sheet |

### Map

Open `sk_unified_map.html` in any browser. Each truck/day is a colored polyline with numbered stops. Real road geometry when OSRM is reachable, straight lines (dashed) when it isn't.

---

## When something's wrong

The validator runs before the solver and prints every problem in plain English, grouped by severity:

```
⛔ ERRORS (must fix):
  1. Client_List row 47: C042 has Tank_lbs = 0 (must be > 0)
  2. Trucks sheet: Truck2 pump_rate_lbs_per_min missing

⚠  WARNINGS (review):
  1. Delivery_Log has 3 rows with customer names that don't match Client_List
  2. Client C099 has a time window (08:00–10:00) narrower than depot shift

ℹ  INFO:
  1. 8 client(s) missing GPS — will be deferred.
```

Errors block the solve. Warnings don't. Run `--validate-only` to check inputs without burning solver time.

---

## Running the test suite

15 synthetic scenarios where the expected result is hand-computable:

```bash
python tests/run_tests.py
```

Covers: trivial one-stop, capacity caps, product splits, urgency priority, hard time windows, closures, missing GPS, shift overflow, depot invariants, double-visit prevention, compartment math, validator happy path, and more. See `tests/README.md` for per-test details.

Before any schema or solver change: run this. If it drops from 15/15, stop and investigate.

---

## Tuning knobs

Most knobs now live in `data/SK_Delivery_System.xlsx` (Depot and Trucks sheets). A few behavior switches still live in `config.py`:

| Constant | What it does |
|---|---|
| `SOLVE_SEC_WEEK` | Default solver time limit (override with `--solve-sec`) |
| `CRITICAL_DAYS` / `URGENT_DAYS` | Urgency bucket thresholds (days-until-empty) |
| `OPPORTUNISTIC_KM` | Max detour to pull a neighbor into a route |
| `PREFERRED_FILL_PCT` / `SOFT_MIN_FILL_PCT` | Scoring floors for how empty a tank should be |
| `BALANCE_WEIGHT` | Load-balance vs. pure urgency preference |
| `COST_PER_MILE` | Fuel + wear estimate used in cost summaries |

---

## Project layout

```
sk_optimizer/
├── run_unified.py          # Entry point
├── config.py               # Paths + solver-behavior constants
├── load_data.py            # Parses Client_List + Delivery_Log
├── schema_loaders.py       # Parses time windows, closures, trucks, depot
├── forecast_consumption.py # Per-client lbs/day estimates from history
├── inventory.py            # Tank-level snapshot as of today
├── router.py               # OSRM distance/time matrix loader
├── unified_solver.py       # OR-Tools weekly PVRP solve
├── validator.py            # Pre-solve input checks with plain-English errors
├── output.py               # Excel + map generation (with polyline cache)
├── state.py                # Optional between-run inventory state
├── data/
│   ├── SK_Delivery_System.xlsx   # THE input file
│   ├── osrm_full_matrix_with_ids.npz
│   └── route_geom_cache.json     # OSRM polyline cache (auto-managed)
├── output/                       # Schedules + maps land here
└── tests/
    ├── test_scenarios.py         # 15 synthetic scenarios
    ├── run_tests.py              # CLI runner
    └── README.md
```
