"""Diagnose why clients stocking out late-horizon are NOT SCHEDULED.

Run: python diagnose_endhorizon.py
"""
import json
from pathlib import Path
import pandas as pd
from load_data import load_all
from config import INPUT_FILE
from irp_core.state_manager import load_plan
from state import load_state

DATA_DIR = Path(__file__).parent / "data"


def main():
    plan = load_plan(DATA_DIR / "plan.json")
    if not plan:
        print("No plan.json — run the optimizer first."); return

    clients_raw, deliveries = load_all(INPUT_FILE)
    from forecast_consumption import estimate_consumption_rates
    enriched = estimate_consumption_rates(deliveries, clients_raw)
    levels = load_state(DATA_DIR / "inventory_state.json")
    # also fall back to Est_Current_lbs from estimator
    est_lvl = dict(zip(enriched["ID"].astype(str), enriched["Est_Current_lbs"]))
    burn = enriched.set_index(enriched["ID"].astype(str))

    plan_dates = pd.to_datetime(plan.get("plan_dates", []))
    horizon_n = len(plan_dates)
    today = pd.to_datetime(plan.get("today"))
    last_day = plan_dates[-1] if len(plan_dates) else today

    scheduled_ids = {str(v["client_id"]) for v in plan.get("visits", [])}
    deferred_ids = {str(x) for x in plan.get("deferred_ids", [])}
    visits_by_id = {}
    for v in plan.get("visits", []):
        visits_by_id.setdefault(str(v["client_id"]), []).append(int(v["day"]))

    rows = []
    for cid, row in burn.iterrows():
        cid = str(cid)
        tank = float(row.get("Tank_lbs", 0) or 0)
        rate = float(row.get("Avg_LbsPerDay", 0) or 0)
        cur = float(levels.get(cid) if levels.get(cid) is not None else est_lvl.get(cid, 0) or 0)
        if rate <= 0 or tank <= 0 or pd.isna(rate) or pd.isna(cur):
            continue
        dte = float(cur) / float(rate)
        if pd.isna(dte) or dte < 0 or dte > 60:
            continue
        stockout_date = today + pd.Timedelta(days=dte)
        # Only care about clients that stock out within the plan horizon
        if stockout_date > last_day + pd.Timedelta(days=2):
            continue
        days = visits_by_id.get(cid, [])
        if days:
            status = f"scheduled day {min(days)}"
        elif cid in deferred_ids:
            status = "deferred (solver dropped)"
        else:
            status = "absent (filtered before solve)"
        rows.append({
            "id": cid,
            "customer": row.get("Customer", ""),
            "zone": row.get("Zone", ""),
            "tank": int(tank),
            "current": int(cur),
            "pct": round(cur / tank * 100, 0),
            "dte": round(dte, 1),
            "stockout_date": stockout_date.strftime("%a %b %d"),
            "stockout_day_idx": (stockout_date - today).days,
            "status": status,
        })

    df = pd.DataFrame(rows).sort_values("dte")
    if df.empty:
        print("No at-risk clients in horizon. ✓"); return

    # Bucket by status
    print(f"\nHorizon: {today.date()} → {last_day.date()} ({horizon_n} workdays)")
    print(f"At-risk clients (stockout within horizon): {len(df)}\n")

    by_status = df.groupby("status").size().to_dict()
    for s, n in by_status.items():
        print(f"  {n:3d}  {s}")

    print("\n── Late-horizon NOT SCHEDULED (stockout days 5+) ──")
    late = df[(df["stockout_day_idx"] >= 5) & df["status"].str.contains("deferred|absent")]
    if late.empty:
        print("  none"); return
    print(late[["id", "customer", "zone", "pct", "dte", "stockout_date", "status"]].to_string(index=False))

    print("\n── End-of-horizon bias check ──")
    # For each scheduled visit, what day is it in horizon?
    sched_days = [d for v in plan.get("visits", []) for d in [int(v["day"])]]
    if sched_days:
        sd = pd.Series(sched_days)
        print("Visits per horizon day:")
        for d in range(horizon_n):
            n = (sd == d).sum()
            bar = "█" * n
            print(f"  day {d} ({plan_dates[d].strftime('%a %m-%d')}): {n:2d} {bar}")

    # Save
    df.to_csv(DATA_DIR / "diagnose_endhorizon.csv", index=False)
    print(f"\nSaved: {DATA_DIR / 'diagnose_endhorizon.csv'}")


if __name__ == "__main__":
    main()
