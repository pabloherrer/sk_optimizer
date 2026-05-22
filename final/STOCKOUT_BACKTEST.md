# STOCKOUT BACKTEST — sk_solver_final vs Tammy (8-week real history)

**Test weeks:** 18 (Tuesdays 2026-01-06 → 2026-05-05)

## Headline numbers

| Metric | Solver | Tammy (actual) | Delta |
|---|---|---|---|
| Total stockout-events (lower = better) | **150** | 376 | -226 |
| Total lbs delivered (sum 18 wks) | 1,027,463 | 1,042,952 | -15,489 |
| Avg stops/week | 84.7 | 83.7 | +1.0 |

**✓ Solver matches or beats Tammy** on stockout prevention (150 ≤ 376).

## Per-week detail

| Week | Solver stops | Solver lbs | Solver stockouts | Tammy stops | Tammy lbs | Tammy stockouts |
|---|---|---|---|---|---|---|
| 2026-01-06 | 81 | 49,313 | **2** | 57 | 37,322 | 13 |
| 2026-01-13 | 88 | 59,839 | **9** | 95 | 74,250 | 22 |
| 2026-01-20 | 93 | 62,139 | **6** | 90 | 59,792 | 12 |
| 2026-01-27 | 78 | 54,515 | **3** | 80 | 54,094 | 17 |
| 2026-02-03 | 86 | 59,396 | **6** | 89 | 59,336 | 17 |
| 2026-02-10 | 81 | 54,759 | **7** | 87 | 64,175 | 21 |
| 2026-02-17 | 85 | 54,324 | **7** | 78 | 51,266 | 20 |
| 2026-02-24 | 86 | 59,148 | **6** | 88 | 60,340 | 17 |
| 2026-03-03 | 89 | 58,399 | **11** | 91 | 60,165 | 16 |
| 2026-03-10 | 86 | 60,703 | **15** | 88 | 63,920 | 25 |
| 2026-03-17 | 88 | 59,040 | **3** | 84 | 61,524 | 25 |
| 2026-03-24 | 88 | 55,338 | **6** | 89 | 64,382 | 21 |
| 2026-03-31 | 80 | 52,643 | **8** | 86 | 54,668 | 22 |
| 2026-04-07 | 69 | 47,711 | **12** | 81 | 55,923 | 31 |
| 2026-04-14 | 84 | 59,948 | **14** | 77 | 54,920 | 26 |
| 2026-04-21 | 84 | 57,967 | **11** | 88 | 57,491 | 24 |
| 2026-04-28 | 92 | 62,885 | **15** | 83 | 55,714 | 24 |
| 2026-05-05 | 86 | 59,396 | **9** | 75 | 53,670 | 23 |

## Customers most frequently stocked out under solver plan

- **DUR - 4029 - DURVILL FOODS** — 8 stockout-events across 18 weeks
- **ECH - 5006 - ECHO 5 SPORTS BAR** — 8 stockout-events across 18 weeks
- **BOOW - 2017 - BOOTY'S WATSON** — 6 stockout-events across 18 weeks
- **THE - 20088 - THE VIG BELL** — 5 stockout-events across 18 weeks
- **BOO - 2023 - BOOTY GOODYEAR** — 4 stockout-events across 18 weeks
- **ATL - 1081 - ATL WINGS ROOSEVELT** — 4 stockout-events across 18 weeks
- **STA - 19123 - STATE 48 HAPPY VALLEY** — 4 stockout-events across 18 weeks
- **MAS - 13086 - MASON'S FAMOUS LOBSTER ROLLS** — 3 stockout-events across 18 weeks
- **ARRM - 1013 - ARRIBAS GOODYEAR** — 3 stockout-events across 18 weeks
- **FAT - 6109 - FATE BREWING TEMPE** — 3 stockout-events across 18 weeks
