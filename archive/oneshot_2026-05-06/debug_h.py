#!/usr/bin/env python3
"""Debug why H column is empty for 8 clients — bulk read version."""
import xlwings as xw

app = xw.apps.active
wb = app.books.active
ws_dl = wb.sheets["Delivery_Log"]

# Bulk read B and J columns
print("Bulk reading Delivery_Log B and J...")
b_col = ws_dl.range('B4:B2000').value  # client IDs
j_col = ws_dl.range('J4:J2000').value  # cons/day
d_col = ws_dl.range('D4:D2000').value  # qty delivered
i_col = ws_dl.range('I4:I2000').value  # days since last

check_ids = ['1', '4037', '12053', '2010', '19123', '3028', '3083', '12062']

for cid in check_ids:
    print(f"\n--- ID {cid} ---")
    for idx in range(len(b_col)):
        b = b_col[idx]
        if b is None:
            continue
        b_str = str(b).replace('.0', '') if '.0' in str(b) else str(b)
        if b_str == cid:
            j = j_col[idx]
            d = d_col[idx]
            i_val = i_col[idx]
            row = idx + 4
            print(f"  Row {row}: B={b}(type={type(b).__name__}), lbs={d}, days_since={i_val}, J={j}")
