# ADR-002: Rolling Horizon Architecture for S&K Route Optimization

**Status:** Proposed  
**Date:** 2026-04-21  
**Author:** Pablo / Claude  

---

## 1. The Problem (What We Tried and Why It Failed)

The current unified solver models the entire week as a single CVRPTW: 103 clients × 2 trucks × 5 days = 30 virtual vehicles, one OR-Tools model. It produces **909 miles for 103 stops** — which is actually good routing.

The problems we wanted to fix:
- **Day imbalance:** Tue/Fri/Sat are heavy, Wed/Thu are light
- **Geographic scatter:** Both trucks crisscross all of Phoenix on the same day

Our attempted fix added ~5 penalty layers (day-balancing fixed costs, angular sweep truck preference, lateness penalties, cluster crossing penalties, graduated deadline constraints). The result: **1,130 miles for 103 stops** — a 24% regression. More driving, same deliveries, zero value added.

**Root cause of failure:** Stacking soft penalties distorted the OR-Tools cost landscape. The solver spent its budget avoiding artificial penalties instead of minimizing actual travel distance. This is a well-known anti-pattern in operations research — overloading a single-model formulation with competing objectives destroys solution quality.

---

## 2. What the Literature Says

This is not a novel problem. S&K's operation is a textbook **Inventory Routing Problem (IRP)** — specifically a Vendor Managed Inventory (VMI) variant where the supplier decides when and how much to deliver, subject to preventing customer stockouts. The field has 30+ years of published research.

### 2.1 The Canonical IRP

Coelho, Cordeau & Laporte (2014) provide the definitive survey in *"Thirty Years of Inventory Routing"* (Transportation Science). The IRP combines three decisions:

1. **When** to visit each customer (day assignment)
2. **How much** to deliver (quantity decision)
3. **What route** to drive (vehicle routing)

Solving all three simultaneously in a single model is NP-hard and computationally intractable for real instances. Every practical system decomposes the problem.

### 2.2 The Two-Phase Decomposition (Campbell & Savelsbergh, 2004)

The most influential practical approach, published in Transportation Science:

- **Phase 1 — Day Assignment:** An integer program decides which clients get visited on which days, considering inventory levels, consumption rates, vehicle capacity, and stockout prevention. This phase does NOT route — it uses cost approximations.
- **Phase 2 — Daily Routing:** For each day, solve an independent CVRPTW with the assigned clients. This is where OR-Tools excels.

This decomposition works because the day-assignment decision and the routing decision have different structures. Day assignment is a scheduling/inventory problem. Routing is a spatial/geometric problem. Combining them into one model forces the solver to balance incompatible objectives.

**This is exactly what Pablo intuited:** "we might be ok to just calculate one day at a time, but we must have a planned future."

### 2.3 Rolling Horizon Framework (Jaillet, Bard, Huang & Dror, 2002)

For ongoing operations (not one-shot planning), the rolling horizon approach from Transportation Science:

1. Plan a window of N days (e.g., 5 days)
2. **Execute only Day 1** — commit those routes
3. Tomorrow, roll forward: re-plan Days 2–6 with updated inventory data
4. Repeat daily

The key insight: **you always plan more days than you execute.** This prevents the "end of horizon effect" (Ben Ahmed et al., 2022) where the solver makes myopic decisions on the last planned day because it can't see what comes next.

For S&K: plan 5 days, execute 1, re-plan daily. The future 4 days act as a lookahead that prevents today's routing from leaving tomorrow with impossible constraints.

### 2.4 Cost Approximation for Future Days

You don't need perfect routing solutions for future days — just reasonable cost estimates. Jaillet et al. (2002) showed that simple delivery cost approximations (based on distance-to-depot and expected cluster size) work well enough for the day-assignment phase. The routing optimizer only needs to run on the committed day.

### 2.5 The Consistent VRP (Groër, Golden & Wasil, 2009)

The ConVRP addresses geographic consistency — the same driver serves the same territory over time. This maps directly to S&K's two-truck operation:

- Assign each client to a **preferred truck** based on geography (stable assignment, updated infrequently)
- The day-assignment phase respects truck preference as a soft constraint
- The daily routing phase already produces geographically tight routes because each truck's client pool is spatially coherent

This solves the geographic scatter problem **at the assignment level**, not by penalizing the router.

### 2.6 Real-World Fuel/Oil Delivery Systems

ORTEC Inventory Routing (the industry leader for oil/gas VMI) uses exactly this decomposition:
- Demand forecasting (consumption rates → stockout prediction)
- Inventory-driven order generation (which clients need delivery)  
- Route optimization (daily CVRPTW)

They report 5–10% cost reduction over manual dispatch and 30% cost-per-liter reduction. Their system re-plans daily with a multi-day lookahead.

---

## 3. Recommended Architecture for S&K

### Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    ROLLING HORIZON LOOP                      │
│                                                             │
│  Each evening (or morning):                                 │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │   PHASE 1    │    │   PHASE 2    │    │   COMMIT     │  │
│  │              │    │              │    │              │  │
│  │ Day Assign-  │───>│ Route Day 1  │───>│ Lock Day 1   │  │
│  │ ment for     │    │ (OR-Tools    │    │ routes for   │  │
│  │ Days 1..5    │    │  CVRPTW)     │    │ dispatch     │  │
│  │              │    │              │    │              │  │
│  │ (LP/greedy   │    │ Route Day 2  │    │ Days 2-5     │  │
│  │  heuristic)  │    │ (lightweight │    │ are tentative│  │
│  │              │    │  estimate)   │    │ (re-planned  │  │
│  │              │    │              │    │  tomorrow)   │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                             │
│  Tomorrow: update inventory, roll forward, repeat           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Phase 1: Day Assignment (the "planned future")

**Input:** Client snapshot with inventory levels, consumption rates, tank sizes, days-until-stockout, truck preference (geographic), closures, time windows.

**Decision:** For each client, assign to a day and truck (or defer). This is a **scheduling** problem, not a routing problem.

#### The Visit Value Score

The key insight from the IRP literature (Archetti et al. TOP-IRP; Campbell & Savelsbergh "Delivery Volume Optimization", Transportation Science 2004; Cornillier et al. PSRPTW) is that the day assignment must go beyond just "serve the stockout clients." A good assignment maximizes the **total value delivered per mile driven across the week**. This is the "logistic ratio" concept from the literature.

Every client on every feasible day gets a **Visit Value Score** that captures both why we *need* to visit and why it's *economically valuable* to visit:

```
VisitValue(client, day) = UrgencyScore + FillScore + ConsolidationBonus
```

**Component 1 — Urgency Score (stockout prevention, the floor)**

Based on projected days-to-empty (DTE) at the visit day, using the existing `project_level()` and `urgency_tier()` functions:

| Projected DTE at visit | Tier | Urgency Score |
|------------------------|------|---------------|
| ≤ 0 (already empty) | stockout | 1000 |
| ≤ 1 day | critical | 800 |
| ≤ 3 days | critical | 600 |
| ≤ 5 days | urgent | 300 |
| ≤ 10 days | moderate | 100 |
| > 10 days | normal | 0 |

This ensures stockout clients always rank highest, but it's only one component.

**Component 2 — Fill Score (the "bigger drop = better economics" principle)**

From propane/oil industry practice: a stop costs ~$50 fixed regardless of how much you deliver (driver time, truck wear, paperwork). The marginal value of a stop scales with *both* how full you fill the tank (efficiency) and how many absolute lbs you deliver (truck utilization). A 240-lb drop on a 300-lb tank is 80% fill but barely dents the truck. A 4,000-lb drop on a 5,000-lb tank is 80% fill AND uses 40% of truck capacity — far more valuable.

```python
projected_refill = tank_lbs - project_level(current_lbs, rate, day_offset, tank_lbs)
fill_pct = projected_refill / tank_lbs          # 0.0–1.0 (stop efficiency)
volume_factor = projected_refill / TRUCK_CAPACITY  # 0.0–1.0 (truck utilization)
fill_score = (fill_pct * 100) + (volume_factor * 100)  # max 200 points
```

The two sub-components capture different things:
- **fill_pct** rewards visiting when the tank is low (efficient use of the stop — you deliver a lot relative to the tank)
- **volume_factor** rewards visiting clients with big tanks (efficient use of the truck — a 5,000-lb tank at 20% uses 40% of truck capacity in one stop; a 300-lb tank at 20% uses 2.4%)

Together they naturally favor the high-value stops: big tanks that are mostly empty. By scoring fill, the assignment naturally delays visits to maximize drop size, which is exactly the "deliver later for a fuller load" principle from IRP theory (Archetti, Bertazzi & Speranza's ML vs OU policy comparison showed the maximum-level policy outperforms order-up-to because it gives the supplier freedom on delivery quantity).

**Component 3 — Consolidation Bonus (the "on the way" value)**

This is the critical component that prevents the greedy heuristic from becoming myopic. For each candidate day, look at who is already assigned:

```python
# How many already-assigned clients on this truck-day are within 5 miles?
nearby_count = count_assigned_within_radius(client, truck, day, radius=5.0)
consolidation_bonus = nearby_count * 50  # up to ~250 for a dense cluster
```

This is the mechanism that captures opportunistic stops. A "normal" client with DTE=12 and fill=40% would score low on urgency (0) and fill (80) alone — probably deferred. But if three other clients on the same truck-day are within 3 miles, the consolidation bonus adds 150, making total score 230 — now worth including. This is mathematically equivalent to what OR-Tools does when it picks up a nearby client "for free" in the unified model, but computed explicitly at the assignment level.

The consolidation bonus also implicitly solves the geographic coherence problem: clients near each other reinforce each other's scores, creating natural geographic clusters per day.

#### The Assignment Algorithm

Two passes, not one:

**Pass 1 — Mandatory clients (score ≥ 600 on any day):**

These are stockout and critical clients. They MUST be served this week.

1. For each mandatory client, compute VisitValue across all feasible (day, truck) slots
2. Assign to the slot with the **highest score** that has capacity
3. If preferred truck is full, allow the other truck (urgency overrides territory)
4. Update capacity budgets after each assignment

Sort order within Pass 1: by max urgency score descending (worst-off clients first), then by number of feasible slots ascending (most constrained clients first — this is the "first-fail" principle from constraint programming).

**Pass 2 — Valuable non-mandatory clients (score < 600):**

These are the clients Pablo is asking about — not at stockout risk, but worth visiting.

1. For each remaining client, compute VisitValue across all feasible (day, truck) slots
2. **Include the consolidation bonus** (which now reflects Pass 1 assignments)
3. Sort by best available score descending
4. For each client: assign to best (day, truck) slot if score exceeds a **minimum threshold** (e.g., 80) AND the slot has remaining capacity
5. After assignment, recompute consolidation bonuses for nearby unassigned clients (they just got more valuable because a neighbor was added)

The threshold of 80 prevents assigning clients that are truly not worth visiting this week — a client at 80% tank, no urgency, no nearby stops, no cadence pressure. Those get deferred properly.

**Pass 2 iteration:** After the initial Pass 2 sweep, do one more pass over deferred clients with updated consolidation bonuses. A client that was below threshold in the first sweep may now be above it because its neighbors got assigned. This converges quickly (2-3 iterations).

#### Capacity Budgets and End-of-Horizon Protection

Each truck-day gets a capacity budget:

```
lbs_budget[truck][day] = TRUCK_CAPACITY  # 10,000 lbs
time_budget[truck][day] = SHIFT_MIN + OT_BUFFER  # 600 + 60 min
```

**End-of-horizon protection (from Ben Ahmed et al., 2022):** For the last planned day (Day 5), reduce the budget:

```
lbs_budget[truck][4] *= 0.80   # reserve 20% for next-week spillover
time_budget[truck][4] *= 0.85
```

This prevents the assignment from greedily filling the last day and creating an impossible Monday. The reserved capacity represents clients who will become urgent between Day 5 and the next plan's Day 1.

#### Why Not an IP for Phase 1?

For 103 clients × 5 days × 2 trucks, a greedy heuristic with consolidation bonuses runs in <100ms and is transparent/debuggable. An IP would be theoretically more optimal but:
- Harder to tune (objective function weights become the same problem we had with penalty stacking)
- Less transparent (hard to explain to S&K why a client was assigned to Thursday)
- Overkill for the problem size

Start greedy. If A/B testing shows the greedy solution leaves >5% value on the table vs. a relaxed LP bound, upgrade to IP later.

### Phase 2: Daily Routing (OR-Tools CVRPTW)

**Input:** The clients assigned to Day 1 (from Phase 1), distance/time matrix, truck capacities, compartment configs, time windows, depot config.

**Decision:** Build routes for Truck 2 and Truck 9 for tomorrow.

**Algorithm:** The existing OR-Tools CVRPTW solver — this is what it's designed for. Single-day, 2 trucks, ~20-25 clients per day. The solver handles:
- Capacity constraints (2 × 5,000 lbs per truck, compartment configs)
- Time windows
- Service times
- Overtime (soft shift limit)
- Disjunction penalties for optional clients

**Why this works better than the unified model:** OR-Tools is solving a much simpler problem — 20-25 nodes instead of 103. The solution space is orders of magnitude smaller. The solver can find near-optimal routes in seconds. No penalty stacking needed because the day assignment already handled the scheduling logic.

**Opportunistic fill preserved:** Phase 1 assigns clients to days but the daily router can still pick up "bonus" clients. If Phase 1 assigned 22 clients to Tuesday but left 5 low-priority clients unassigned, add those 5 as optional nodes (with disjunction penalties) in the Day 1 model. OR-Tools will pick them up if they're on the way. This preserves the opportunistic detour behavior that makes the unified approach valuable.

### Truck Territory Assignment (stable, updated monthly)

Compute once, store in config:
- Calculate the angular position of each client relative to the depot
- Split into two roughly equal groups by demand
- Assign each group to a truck

This is a **soft** preference — Phase 1 respects it when possible but overrides for urgency. The daily router doesn't need to know about it at all, because the client pool it receives is already geographically coherent from Phase 1.

---

## 4. What Changes in the Code

### Keep (unchanged):
- `load_data.py` — data loading
- `forecast_consumption.py` — consumption rate estimation
- `inventory.py` — snapshot enrichment
- `router.py` — distance/time matrix loading
- `state.py` — state management
- `schema_loaders.py` — constraint loading
- `output.py` — Excel/map output
- `validator.py` — input validation
- `config.py` — configuration (add a few new params)

### Modify:
- **`unified_solver.py`** → refactor `solve_week()` into two clear phases:
  - `assign_days()` — Phase 1 greedy day assignment
  - `route_day()` — Phase 2 single-day CVRPTW (mostly the existing OR-Tools code, simplified)
  - `solve_week()` becomes a thin wrapper that calls assign_days() then route_day() for each day

### New (small):
- **`territory.py`** — angular sweep truck assignment (compute once, cache)

### Size estimate:
- `assign_days()`: ~150-200 lines (greedy heuristic)
- `route_day()`: ~400-500 lines (simplified from current 1,411 — no virtual vehicle mapping, no multi-day penalty stacking)
- `territory.py`: ~50 lines
- Net: roughly the same total code, but cleaner separation of concerns

---

## 5. Expected Results

| Metric | Current (HEAD) | Expected (Rolling Horizon) |
|--------|---------------|---------------------------|
| Total miles | 909 | ~900-950 (similar or slightly better) |
| Day balance | Uneven (10-26 stops/day) | Even (~18-22 stops/day) |
| Geographic coherence | Both trucks everywhere | Each truck in its territory |
| Stockout prevention | Good (0 critical deferred) | Same or better |
| Solver time | 30s (one big model) | <5s per day × 5 = <25s total |
| Debuggability | Hard (30 virtual vehicles) | Easy (why was client X on Tuesday? Check Phase 1 log) |

The key insight: day balance and geographic coherence come from **Phase 1 (assignment)**, not from penalty-loading the router. The router's only job is to find the shortest path through the day's assigned clients. Each component does one thing well.

---

## 6. Implementation Plan

1. **Extract `route_day()` from the existing `solve_week()`** — strip out the multi-day virtual vehicle mapping, keep the core OR-Tools CVRPTW logic. Test: single-day routing should produce identical results to one day-slice of the current output.

2. **Build `assign_days()`** — greedy priority heuristic. Test: all critical/stockout clients assigned to Day 0 or 1. Capacity budgets respected. No client left behind unless truly no capacity.

3. **Build `territory.py`** — angular sweep. Test: two roughly balanced halves of Phoenix.

4. **Wire into `solve_week()`** — Phase 1 assigns, Phase 2 routes each day. Compare full-week output against baseline.

5. **A/B test** — run both architectures on the same input, compare miles, stops, balance, geographic coherence.

---

## 7. References

- Coelho, L.C., Cordeau, J.F. & Laporte, G. (2014). "Thirty Years of Inventory Routing." *Transportation Science*, 48(1), 1-19.
- Campbell, A.M. & Savelsbergh, M.W.P. (2004). "A Decomposition Approach for the Inventory-Routing Problem." *Transportation Science*, 38(4), 488-502.
- Jaillet, P., Bard, J.F., Huang, L. & Dror, M. (2002). "Delivery Cost Approximations for Inventory Routing Problems in a Rolling Horizon Framework." *Transportation Science*, 36(3), 292-300.
- Groër, C., Golden, B. & Wasil, E. (2009). "The Consistent Vehicle Routing Problem." *Manufacturing & Service Operations Management*, 11(4), 630-643.
- Ben Ahmed, W., Gicquel, C. & Klemmt, A. (2022). "Long-term effects of short planning horizons for inventory routing problems." *International Transactions in Operational Research*, 29(3).
- Archetti, C. & Speranza, M.G. (2016). "The inventory routing problem: the value of integration." *International Transactions in Operational Research*.
