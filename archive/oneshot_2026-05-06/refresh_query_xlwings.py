#!/usr/bin/env python3
"""Fetch fresh Anova data from Azure and write to Query sheet via xlwings."""
import xlwings as xw
import json, urllib.request, time
from datetime import datetime, timedelta

# Arizona is always UTC-7 (no daylight saving)
AZ_OFFSET = timedelta(hours=-7)

AZURE_URL = "https://skoil-anova-escpe7axeyczdshp.westcentralus-01.azurewebsites.net/api/anova/data"
HEADERS = ["device_id", "customer", "asset_id", "display_value", "display_unit",
           "scaled_value", "scaled_unit", "product", "timestamp", "received_at"]

# Fetch fresh data
print("Fetching from Azure...")
req = urllib.request.Request(AZURE_URL)
with urllib.request.urlopen(req, timeout=15) as resp:
    data = json.loads(resp.read())

# Filter to Level only
readings = [r for r in data if r.get("channel_type", "") == "Level"]
print(f"  Got {len(readings)} Level readings")
for r in readings:
    print(f"    {r.get('device_id')}: {r.get('display_value')} lbs @ {r.get('timestamp')}")

# Write to Query sheet via xlwings
app = xw.apps.active
wb = app.books.active
ws = wb.sheets["Query"]

# Write headers
ws.range("A1").value = HEADERS
print(f"\nWriting {len(readings)} rows to Query sheet...")

# Clear old data (10 columns now: A-J)
ws.range("A2:J200").clear_contents()

# Write new data
for i, reading in enumerate(readings):
    row = i + 2
    for j, field in enumerate(HEADERS):
        val = reading.get(field, "")
        if val is None:
            val = ""
        # Convert ISO timestamps to Phoenix time (UTC-7, no DST in AZ)
        if field in ("timestamp", "received_at") and isinstance(val, str) and "T" in val:
            try:
                utc_dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                val = utc_dt.replace(tzinfo=None) + AZ_OFFSET
            except:
                pass
        ws.range((row, j + 1)).value = val

time.sleep(1)
app.calculate()
time.sleep(3)

# Verify chain
ws_al = wb.sheets["Anova_Live"]
ws_cl = wb.sheets["Client_List"]
ws_oi = wb.sheets["Optimizer_Input"]

print("\n=== VERIFY ===")
print(f"Anova_Live E2(lbs): {ws_al.range('E2').value}")
print(f"Anova_Live K2(pct): {ws_al.range('K2').value}")
print(f"Anova_Live M2(age_hrs): {ws_al.range('M2').value}")
print(f"Client_List S8(level): {ws_cl.range('S8').value}")
print(f"Client_List W8(age): {ws_cl.range('W8').value}")

for row in range(6, 30):
    b = ws_oi.range(f"B{row}").value
    if b and "1054" in str(b):
        print(f"Opt_Input row {row}: S={ws_oi.range(f'S{row}').value}, U={ws_oi.range(f'U{row}').value}")
        break

wb.save()
print("\nSaved. Data is fresh.")
