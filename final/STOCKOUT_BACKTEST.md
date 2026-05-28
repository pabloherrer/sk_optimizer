# STOCKOUT BACKTEST — sk_solver_final vs Tammy (8-week real history)

**Test weeks:** 5 (Tuesdays 2026-04-07 → 2026-05-05)

## Headline numbers

| Metric | Solver | Tammy (actual) | Delta |
|---|---|---|---|
| Total stockout-events (lower = better) | **86** | 132 | -46 |
| Total lbs delivered (sum 18 wks) | 296,267 | 277,718 | +18,549 |
| Avg stops/week | 86.2 | 80.8 | +5.4 |

**✓ Solver matches or beats Tammy** on stockout prevention (86 ≤ 132).

## Per-week detail

| Week | Solver stops | Solver lbs | Solver stockouts | Tammy stops | Tammy lbs | Tammy stockouts |
|---|---|---|---|---|---|---|
| 2026-04-07 | 87 | 61,189 | **12** | 81 | 55,923 | 33 |
| 2026-04-14 | 81 | 53,691 | **20** | 77 | 54,920 | 26 |
| 2026-04-21 | 85 | 57,704 | **23** | 88 | 57,491 | 26 |
| 2026-04-28 | 87 | 60,671 | **18** | 83 | 55,714 | 24 |
| 2026-05-05 | 91 | 63,012 | **13** | 75 | 53,670 | 23 |

## Customers most frequently stocked out under solver plan

- **ATL - 1083 - ATL WINGS MESA** — 4 stockout-events across 18 weeks
- **ECH - 5006 - ECHO 5 SPORTS BAR** — 3 stockout-events across 18 weeks
- **OREL - 15033 - OREGANO LANDING** — 3 stockout-events across 18 weeks
- **CRO - 3064 - CROWNE PLAZA RESORT** — 2 stockout-events across 18 weeks
- **KOD - 11013 - K O'DONNELS SPORTS BAR & GRILL** — 2 stockout-events across 18 weeks
- **MAN9 - 13014 - MANUEL SCOTTSDALE** — 2 stockout-events across 18 weeks
- **OREG - 15024 - OREGANO GILBERT** — 2 stockout-events across 18 weeks
- **STA - 19035 - STATE 48 GLENDALE** — 2 stockout-events across 18 weeks
- **TAI - 20089 - TAILGATERS PRESCOTT** — 2 stockout-events across 18 weeks
- **THE - 20088 - THE VIG BELL** — 2 stockout-events across 18 weeks
