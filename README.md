# S&K Route Optimizer

A production solver for the **Inventory Routing Problem (IRP)** at S&K Oil Sales. Combines OR-Tools CVRP, live tank sensors (Anova Transcend), chance-constrained urgency forecasting, and a rolling-horizon plan that re-solves daily.

---

## The problem

S&K delivers cooking oil to ~170 restaurants across the Phoenix metro. Two trucks (Truck2 and Truck9), 5 working days a week (Tue–Sat). Every restaurant has a tank that drains at its own rate; some have live sensors, some don't.

**Three decisions, one optimization, every afternoon:**

1. **Inventory** — which tanks need oil within the planning horizon?
2. **Routing** — what's the cheapest way to visit them?
3. **Scheduling** — which day does each delivery happen?

These are coupled: deliver too early and you waste pump time on partial fills; deliver too late and the restaurant runs dry. The classical IRP formulation (Coelho-Cordeau-Laporte 2014) is what this solver implements.

---

## Architecture

```
                              ┌─────────────────────┐
   SK_Delivery_System.xlsx ──▶│   ingest/           │
   (Client_List, Delivery_Log,│   excel, schema,    │
    Time_Windows, Closures,   │   matrix, anova,    │
    Trucks, Depot,            │   actuals, notes    │
    Anova Query sheet)        └──────────┬──────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │   forecast/         │
   Anova sensor readings ────▶│   consumption,      │
   (live + projected stale)   │   demand_model,     │
                              │   safety_stock      │
                              └──────────┬──────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
   inventory_state.json ─────▶│   state/            │
   (atomic v2 persistence)    │   manager,          │
   plan.json (warm start) ───▶│   warm_start        │
                              └──────────┬──────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │   solver/           │
                              │   core (OR-Tools),  │
                              │   objective,        │
                              │   economics         │
                              └──────────┬──────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │   reporting/        │
                              │   writers (Excel +  │
                              │   map), smartservice│
                              │   diagnostics       │
                              └──────────┬──────────┘
                                         │
                                         ▼
        sk_irp_schedule.xlsx │ sk_irp_map.html │ sk_irp_smartservice.csv
```

**Single solver, single entry point.** The OR-Tools CVRP is `solver.core.solve_horizon`. The CLI entry is `run.py`. The web UI is `app.py`. See [adr/ADR-002](adr/ADR-002_Single_Entry_Point.md).

---

## Folder layout

| Path | Purpose |
|---|---|
| `app.py` | Flask web UI ("Generate Routes" button + dashboard) |
| `run.py` | CLI entry — composes the IRP pipeline |
| `backtest.py` | A/B harness for solver comparison |
| `config.py` | All tunable constants — change here, nothing else |
| `inventory.py` | Pure inventory math (project_level, refill, urgency) |
| `validation.py` | Pre-solve input validation |
| **`ingest/`** | Data loaders (Excel, schemas, matrix, Anova, actuals, notes) |
| **`forecast/`** | Demand modeling (consumption rates, empirical-Bayes DOW model, P95 safety stock) |
| **`state/`** | Inventory state persistence + warm-start (atomic v2 JSON) |
| **`solver/`** | OR-Tools CVRP, objective calibration, $-economics |
| **`reporting/`** | Excel + map + SmartService CSV + plan-quality diagnostics |
| **`scripts/`** | Operational utilities (anova_fetch, refresh_anova, build_matrix, add_client, anova_history) |
| **`adr/`** | Architecture Decision Records |
| **`docs/`** | Design notes + history (older IRP_*.md reports) |
| **`archive/`** | Dead code and legacy entry points kept for reference |

---

## Quick start

### Web UI (recommended)
```bash
cd sk_optimizer
python app.py
# Open http://localhost:5050
```

Click **Generate Routes**. Toggle **Cold Start** to wipe state and rebuild from scratch.

### CLI
```bash
python run.py                  # plan from tomorrow forward
python run.py --today 2026-05-08
python run.py --no-warm-start  # cold start
python run.py --dry-run        # don't persist plan/state
python run.py --confirm actuals.csv  # apply driver-confirmed deliveries
```

### Outputs (in `output/`)
- `sk_irp_schedule.xlsx` — full schedule per truck/day + summary + stockout-risk + deferred list
- `sk_irp_map.html` — interactive Folium map with route polylines
- `sk_irp_smartservice.csv` — importable into S&K's existing dispatch system

---

## How the rolling horizon works

Every afternoon the planner runs:

1. **Read live state** — atomic v2 `inventory_state.json` (last persisted) overlaid with live Anova sensor data (fresh ≤24h, projected forward for 24-72h stale readings)
2. **Forecast demand** — empirical-Bayes per-client consumption rates with day-of-week effects; σ̂ tightened for sensor-monitored clients
3. **Build P95 urgency profiles** — chance-constrained "must visit by" deadlines that respect demand uncertainty
4. **Warm-start from yesterday's plan** — shifts day indices to match today's calendar; passes prior routes as solver hints
5. **Solve** the 10-day, 30-vehicle (Truck × Day × Compartment) CVRP with OR-Tools
6. **Commit days 0-1, preview days 2-9** — first two days are dispatched, the rest are tentative lookahead
7. **Persist plan + state atomically** — next afternoon's run continues from here

See [adr/ADR-001_Rolling_Horizon.md](adr/ADR-001_Rolling_Horizon.md).

---

## Key configuration

All in `config.py`:

| Constant | Value | Why |
|---|---|---|
| `HORIZON_DAYS` | 10 | Two full work weeks — enough to spread load across the weekend gap |
| `COMMIT_DAYS` | 2 | Today + tomorrow are firm dispatches; rest is preview |
| `TRUCKS` | Truck2 (152.6 lbs/min), Truck9 (206.0 lbs/min) | 10K capacity each, 2×5K compartments |
| `SATURDAY_TRUCKS` | `['Truck2']` | Truck9 does the alternating Tucson/Flagstaff far run |
| `METRO_CROSS_PENALTY` | 50,000 (~30mi) | Strong deterrent against East→West→East zigzags |
| `LATE_PENALTY_PER_DAY` | 5,000 | ~3 mi/day late equivalent |
| `OT_PENALTY_PER_MIN` | 200 | 1.5x labor + push-back against day-0 cramming |
| `EFFICIENCY_WEIGHT` | 2.5 | Prefer high-fill stops — more lbs per trip |
| `NEIGHBOR_SWEEP_ENABLED` | True | Catches "neighbor delivered earlier than this client" cases |

---

## Decisions documented

- [adr/ADR-001_Rolling_Horizon.md](adr/ADR-001_Rolling_Horizon.md) — why a 10-day rolling horizon with 2-day commit
- [adr/ADR-002_Single_Entry_Point.md](adr/ADR-002_Single_Entry_Point.md) — why one solver and one entry point (this consolidation)
- [adr/ADR-003_Objective_Wiring.md](adr/ADR-003_Objective_Wiring.md) — why we kept legacy magic constants instead of $-calibrated penalties (for now)

---

## Operational notes

- **Anova Power Query** must refresh for live sensor data to flow. The `Query` sheet in `SK_Delivery_System.xlsx` is the source of truth; readings >72h old fall back to estimated state.
- **State drift**: if the system is offline more than a few days, run with **Cold Start** to rebuild from delivery log + sensors.
- **Excel must be closed** when the solver runs — it writes back to the workbook.

---

## Backtest / A/B testing

```bash
python backtest.py            # compare configurations against fixture data
```

See `data/backtest_*.json` for fixtures.
