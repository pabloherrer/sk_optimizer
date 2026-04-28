"""
S&K Oil Sales — Route Optimizer Configuration
==============================================
All tunable parameters live here. Change values in this file only;
nothing else in the codebase needs to be edited for routine tuning.
"""

from pathlib import Path
import sys, os

# ── Force UTF-8 on Windows (prevents CP1252 crash with Unicode chars) ────────
if sys.platform == 'win32':
    os.environ.setdefault('PYTHONUTF8', '1')
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / 'data'
OUTPUT_DIR = BASE_DIR / 'output'

INPUT_FILE  = DATA_DIR / 'SK_Delivery_System.xlsx'
MATRIX_FILE = DATA_DIR / 'osrm_full_matrix_with_ids.npz'
NODES_FILE  = DATA_DIR / 'osrm_nodes_used_with_ids.csv'
STATE_FILE  = DATA_DIR / 'inventory_state.json'

# ── Depot ─────────────────────────────────────────────────────────────────────
# Coordinates come from the matrix file (node 0); these are kept as fallback.
DEPOT_ID  = 'DEPOT'
DEPOT_LAT = 33.5152
DEPOT_LON = -112.1674

# ── Fleet ─────────────────────────────────────────────────────────────────────
# Pump physics:  20 gal/min base × 7.63 lbs/gal = 152.6 lbs/min (Truck2)
# Truck9 is 35% faster:  152.6 × 1.35 = 206.0 lbs/min
# fixed_setup_min: hose-connect + paperwork + disconnect (both ends identical)
TRUCKS = {
    'Truck2': {
        'capacity_lbs':          10_000,   # 2 compartments × 5,000 lbs, interchangeable
        'pump_rate_lbs_per_min': 152.6,
        'fixed_setup_min':       18,       # Total stop ~20 min door-to-door (18 setup + ~2 pump)
    },
    'Truck9': {
        'capacity_lbs':          10_000,   # 2 compartments × 5,000 lbs, interchangeable
        'pump_rate_lbs_per_min': 206.0,
        'fixed_setup_min':       18,
    },
}
TRUCK_NAMES = list(TRUCKS.keys())   # ['Truck2', 'Truck9']
NUM_TRUCKS  = len(TRUCK_NAMES)

# ── Saturday fleet ───────────────────────────────────────────────────────────
# One truck always runs Tucson/Flagstaff on Saturdays, leaving only one truck
# for the metro route. The solver will auto-restrict Saturday to this list.
SATURDAY_TRUCKS = ['Truck2']        # Only Truck2 available for metro Saturdays

# ── Work week ─────────────────────────────────────────────────────────────────
DAYS     = ['Tue', 'Wed', 'Thu', 'Fri', 'Sat']
NUM_DAYS = len(DAYS)

SHIFT_MIN     = 600   # Soft shift cap: 10 hours (minutes). Beyond this is overtime.
MAX_SHIFT_MIN = 720   # Hard driver-hours ceiling: 12 hours (minutes). Routes cannot exceed this.
OVERTIME_MIN  = 480   # Early soft cap: target return 2 PM (8 hours); penalise above this

# ── Overtime cost model ──────────────────────────────────────────────────────
# Shift time beyond SHIFT_MIN is legal but billed 1.5x. The objective adds an
# overtime cost to let the solver trade OT minutes against marginal revenue
# from an extra stop. Expressed in the same integer cost units as distance.
OT_MULTIPLIER        = 1.5    # 1.5x base labor rate for minutes over SHIFT_MIN
LABOR_COST_PER_MIN   = 50     # Base labor cost units per minute (≈ $30/hr driver → 50 units/min)
OT_PENALTY_PER_MIN   = int(LABOR_COST_PER_MIN * (OT_MULTIPLIER - 1.0))  # = 25: the *extra* cost per OT minute

# ── Deadline-coupled routing (ADR-001) ───────────────────────────────────────
# Lateness penalty: cost units added per day a client is served past their
# stockout deadline. Must stay proportional to distance — too high and the
# solver ignores route geometry chasing penalty avoidance.
# ~9 miles equivalent at 15,000.  Range: 10,000–25,000 reasonable.
# 0 = disabled (legacy behavior: day-anonymous objective).
LATE_PENALTY_PER_DAY = 5_000   # ~3 miles/day late — stronger push toward preferred fill day

# Hard deadline slack: how many days past deadline a client can still be
# assigned via hard constraint. 1 = one day of slack (solver CAN serve a
# client one day late, but lateness penalty makes it a last resort).
# 0 = strict (serve by deadline day or don't serve at all — risky with
# tight capacity). Only applies to clients with deadlines within the week.
DEADLINE_SLACK_DAYS = 1

# ── Inventory thresholds ──────────────────────────────────────────────────────
MIN_OIL_PCT      = 0.00   # 0 % floor — stockout protection handled by scheduler urgency
MIN_FILL_PCT     = 0.00   # No hard visit gate — stops below preferred fill are allowed
                          # if they fit the route. Goal is route density + tank efficiency.
PREFERRED_FILL_PCT = 0.85 # Preferred floor: optimizer scores higher-fill deliveries better
SOFT_MIN_FILL_PCT = 0.65  # Soft gate: eligible at 65% empty; solver prefers higher via EW=2.5
CRITICAL_DAYS = 1.5    # Stockout within 1.5 days → mandatory visit today
URGENT_DAYS   = 3.0    # Stockout within 3 days   → high-priority (was 4 — too eager)

# ── Contractual service cadence (from Aksen et al. 2012 SIRP formulation) ────
# Customer contract caps the gap between two successive visits. Any client
# whose last visit would be older than MAX_SERVICE_INTERVAL_DAYS by the END
# of the planning window is elevated to mandatory ("must serve this week")
# regardless of current tank level. This plugs the gap the audit flagged:
# the previous model had no hard upper bound on visit spacing — only urgency
# penalties keyed to stockout — so a client on vacation could quietly slip
# past the 14-day contractual limit.
MAX_SERVICE_INTERVAL_DAYS = 9999  # Disabled — S&K has no contractual max interval

# ── Profit-weighted objective (Cornillier, Boctor, Laporte & Renaud 2009) ────
# PSRPTW frames routing as profit = revenue(lbs delivered) − cost(miles + time).
# We implement a scalar proxy: the drop-penalty of each client is multiplied
# by (1 + EFFICIENCY_WEIGHT × Fill_Pct_At_Visit). A nearly-full tank gets a
# larger drop-penalty (more revenue at stake); a low-fill tank gets less.
# The net effect is that the solver prefers dense, high-fill routes over
# low-fill scatter — which matches S&K's business goal (route density +
# tank efficiency) without abandoning distance minimization.
# 0.0 = pure distance-min (legacy).  1.5 = strongly efficiency-weighted.
EFFICIENCY_WEIGHT = 2.5  # Strongly prefer high-fill stops — more lbs per trip

# ── Time windows enforcement (Cornillier PSRPTW) ──────────────────────────────
# The codebase already loads time_windows_df but never applies it to the
# solver's Time dimension. Flip this to True to activate per-node CumulVar
# bounds on the ~12 clients with morning-only / after-2-PM windows.
ENFORCE_TIME_WINDOWS = True

# ── Forward-projected refills (Coelho-Cordeau-Laporte 2014) ──────────────────
# True  → solver sizes per-client refill using the *end-of-week* projection
#          (refills_by_day[NUM_DAYS-1]). Any client visited on Day 4 must carry
#          the Day-4 refill amount, so this keeps feasibility conservative.
# False → solver sizes each refill from *today's* snapshot. Faster/simpler,
#          but risks under-provisioning trucks for late-week stops.
# Toggleable for A/B benchmarking; production defaults to True.
USE_FORWARD_REFILLS = True

# ── Opportunistic fill ───────────────────────────────────────────────────────
# After the EDF assigns "must serve" clients to each day, sweep the route for
# unserved territory clients whose tank has room.  The truck is already in the
# area — the marginal cost of one more stop is a few km detour and pump time.
# The cost of a separate trip later is the full depot round-trip ($50+).
#
# This is the single most impactful cost lever: never go to the same area twice
# in one week if you can fill everything on the first visit.
OPPORTUNISTIC_KM       = 8.0   # Max detour to pull in a neighbor (km).
                                # 8 km ≈ 10-min drive in Phoenix — trivial.
OPPORTUNISTIC_FILL_PCT = 0.55  # Opportunistic backfill: if truck is already nearby, 55%+ is worth it.
                                # 50 % = tank at least half empty → meaningful delivery.
                                # Below 50 % the pump time may not justify the stop.

# ── Scoring / objective weights ───────────────────────────────────────────────
# Phase-1 visit score:
#   score(i,d) = fill_efficiency × account_weight × urgency_multiplier
#
# Urgency multipliers (applied at projected visit day, not today):
URGENCY_WEIGHTS = {
    'stockout':  20.0,   # Days_until_empty ≤ 0 at visit time
    'critical':  10.0,   # ≤ CRITICAL_DAYS
    'urgent':     3.0,   # ≤ URGENT_DAYS
    'normal':     1.0,
}

# Account importance blending:
#   0.0 = pure throughput (Avg_LbsPerDay)  |  1.0 = pure tank size
ACCOUNT_ALPHA = 0.25   # Slightly favour throughput over tank size

# Day load-balancing incentive (Phase 1 greedy scheduler).
# Multiplies each candidate's score by (1 + BALANCE_WEIGHT × (1 − load_fraction)).
# Empty slot → ×(1+BALANCE_WEIGHT) bonus.  Full slot → no bonus.
# This prevents all eligible clients from piling onto the last day of the week
# (the "due-date clustering" failure mode of pure urgency-first greedy).
# 0.0 = disabled (pure urgency/geo)  |  1.0 = strong balancing preference
BALANCE_WEIGHT = 0.5    # Equal weight: balance vs. geo compactness.
                        # Territory assignment already separates the trucks;
                        # within a territory, geo should have real influence.

MIN_ROUTE_STOPS = 5    # Slots with fewer stops (and no urgent clients) are
                        # deferred to the next day during consolidation.
                        # 5 is the break-even: fixed depot round-trip cost
                        # shared by at least 5 deliveries.

# Phase-2 OR-Tools objective: minimise total route time (time callbacks)
# Distance is implicitly penalised through travel time.

# ── Rolling Horizon (Campbell & Savelsbergh 2004, Jaillet et al. 2002) ────────
# Plan each afternoon for the next HORIZON_DAYS working days. Only the first
# COMMIT_DAYS are dispatched to drivers; the rest are tentative lookahead.
# Re-run each afternoon with updated inventory (actuals replace projections).
HORIZON_DAYS    = 10   # Total planning window (working days). 10 = two full Tue-Sat weeks.
                       # Extended from 5 to fix the cascading-crunch problem: with a 5-day
                       # window, clients at DTE 7-10 are deferred and become next week's
                       # emergencies. A 10-day horizon lets the solver see two full weeks
                       # and spread load across the weekend gap.
COMMIT_DAYS     = 2    # Firm routes dispatched to drivers. 2 = today + tomorrow.
                       # Remaining 8 days are tentative lookahead for capacity planning.
HORIZON_BUFFER  = 3    # Days past horizon end to check for looming stockouts.
                       # Clients whose stockout falls within HORIZON_BUFFER after
                       # the plan ends get escalated disjunction penalties.

# ── Solver ────────────────────────────────────────────────────────────────────
SOLVE_SEC      = 90    # Time limit per day-solve (seconds) — legacy per-day
SOLVE_SEC_WEEK = 600   # Unified solver: 10 min default for 10-day/60-vehicle horizon
                       # (was 300 for 5-day/30-vehicle; doubled for 2x vehicles)
SOLUTION_LIMIT = 1_000 # Early-stop if solver finds this many improving solutions

# OR-Tools strategy overrides (string names, read via getattr on routing_enums_pb2).
# Valid FirstSolutionStrategy names include: PATH_CHEAPEST_ARC, PATH_MOST_CONSTRAINED_ARC,
# SAVINGS, CHRISTOFIDES, PARALLEL_CHEAPEST_INSERTION, LOCAL_CHEAPEST_INSERTION,
# GLOBAL_CHEAPEST_ARC, AUTOMATIC, FIRST_UNBOUND_MIN_VALUE.
# Valid LocalSearchMetaheuristic names include: GUIDED_LOCAL_SEARCH, SIMULATED_ANNEALING,
# TABU_SEARCH, GENERIC_TABU_SEARCH, AUTOMATIC, GREEDY_DESCENT.
FIRST_SOLUTION_STRATEGY   = 'PARALLEL_CHEAPEST_INSERTION'
LOCAL_SEARCH_METAHEURISTIC = 'GUIDED_LOCAL_SEARCH'

# ── Cost ─────────────────────────────────────────────────────────────────────
COST_PER_MILE = 0.14   # USD per mile (fuel + wear)

# ── Products & Compartments ───────────────────────────────────────────────────
# Two active products; each truck has 2 interchangeable compartments of 5,000 lbs.
# Any compartment can hold any single product.
PRODUCTS = ['CANOLA', 'FRYERS CHOICE']
COMPARTMENT_CAPACITY_LBS = 5_000   # Max lbs per compartment

# Raw product name → canonical name (covers common variants in source data)
PRODUCT_ALIASES = {
    'CANOLA OIL':          'CANOLA',
    'CANOLA':              'CANOLA',
    '100% CANOLA':         'CANOLA',
    'FRYERS CHOICE BLEND': 'FRYERS CHOICE',
    'FRYERS CHOICE':       'FRYERS CHOICE',
    'FRYER\'S CHOICE':     'FRYERS CHOICE',
    # Fallbacks for demo data products not yet in scope — map to closest
    'SOYBEAN OIL':         'CANOLA',
    'VEGETABLE OIL':       'FRYERS CHOICE',
}

# ── Unit conversion ──────────────────────────────────────────────────────────
METERS_PER_MILE = 1609.34

# ── Truck speed adjustment ───────────────────────────────────────────────────
# OSRM returns car-speed travel times. Loaded delivery trucks are slower.
# 1.0 = car speed, 1.25 = 25% slower (e.g., 40 min OSRM → 50 min actual).
# Ask drivers what feels right. 1.20–1.35 is typical for urban delivery trucks.
TRUCK_SPEED_FACTOR = 1.25

# ── Excluded regions ─────────────────────────────────────────────────────────
# Tucson & Flagstaff are on a separate bi-weekly Saturday run.
# These clients are excluded from the weekly optimizer entirely.
EXCLUDED_CLIENT_IDS: set = {
    # Flagstaff area (lat ~35.x) — bi-weekly Saturday run
    '11005',  # Karma Sushi
    '12021',  # Lotus Lounge
    '15032',  # Oregano Country
    '15004',  # Oregano Flagstaff
    # Tucson / Casa Grande area (lat ~32.x) — bi-weekly Saturday run
    '1057',   # Angry Crab Tucson
    '10012',  # Jay Travel Center
    '15033',  # Oregano Landing
    '15028',  # Oregano Tucson
    '15021',  # Oregano Speedway
    '16027',  # Pirate Casa Grande
}

# ── Consumption estimation ────────────────────────────────────────────────────
MIN_DELIVERIES_FOR_OWN_RATE = 2    # Need ≥2 deliveries to use a client-specific rate
OUTLIER_IQR_FACTOR          = 3.0  # Flag per-delivery rates > Q3 + 3×IQR as outliers
                                    # (replaces the old hard 500 lbs/day cap)
FALLBACK_DAYS_SINCE         = 14   # Days assumed since last delivery for brand-new clients

# ── Output styling ────────────────────────────────────────────────────────────
TRUCK_HEX = {
    'Truck2': 'FF1A6FAF',   # Blue family
    'Truck9': 'FFC0392B',   # Red family
}
TRUCK_MAP_COLORS = {
    'Truck2': ['#1a6faf', '#2196F3', '#64B5F6', '#0D47A1', '#42A5F5'],
    'Truck9': ['#c0392b', '#E53935', '#EF9A9A', '#B71C1C', '#EF5350'],
}
URGENCY_FILL_COLORS = {
    'stockout':  'FFFF9999',
    'critical':  'FFFFCC99',
    'urgent':    'FFFFFFAA',
    'normal':    'FFCCFFCC',
    'deferred':  'FFE0E0E0',
}
