"""
S&K Oil Sales — Route Optimizer Configuration
==============================================
All tunable parameters live here. Change values in this file only;
nothing else in the codebase needs to be edited for routine tuning.
"""

from pathlib import Path

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

# ── Work week ─────────────────────────────────────────────────────────────────
DAYS     = ['Tue', 'Wed', 'Thu', 'Fri', 'Sat']
NUM_DAYS = len(DAYS)

SHIFT_MIN    = 600   # Hard shift cap: 10 hours (minutes) — 6 AM to 4 PM
OVERTIME_MIN = 480   # Soft cap: target return 2 PM (8 hours); penalise above this

# ── Inventory thresholds ──────────────────────────────────────────────────────
MIN_OIL_PCT      = 0.00   # 0 % floor — stockout protection handled by scheduler urgency
MIN_FILL_PCT     = 0.00   # No hard visit gate — stops below preferred fill are allowed
                          # if they fit the route. Goal is route density + tank efficiency.
PREFERRED_FILL_PCT = 0.85 # Preferred floor: optimizer scores higher-fill deliveries better
SOFT_MIN_FILL_PCT = 0.60  # Soft gate: clients below 60 % fill get discounted score only
CRITICAL_DAYS = 1.5    # Stockout within 1.5 days → mandatory visit today
URGENT_DAYS   = 4.0    # Stockout within 4 days   → high-priority

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
OPPORTUNISTIC_FILL_PCT = 0.50  # Minimum fill to justify the stop.
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

# ── Solver ────────────────────────────────────────────────────────────────────
SOLVE_SEC      = 90    # Time limit per day-solve (seconds) — legacy per-day
SOLVE_SEC_WEEK = 300   # Unified solver: 5 min default (up to 1800 for production)
SOLUTION_LIMIT = 1_000 # Early-stop if solver finds this many improving solutions

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
    # Flagstaff area (lat ~35.x)
    'C057',   # Karma Sushi Bar & Grill
    'C164',   # Lotus Lounge
    'C166',   # Oregano Country
    'C169',   # Oregano Flagstaff
    # Tucson area (lat ~32.x)
    'C158',   # Angry Crab Tucson
    'C163',   # Jay Travel Center
    'C167',   # Oregano Landing
    'C168',   # Oregano Tucson
    'C170',   # Oregano Speedway
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
