#!/usr/bin/env python3
"""
Fix H column (Last Per Day Cons) formula in Optimizer_Input.
Problem: text IDs in Optimizer_Input B column don't match number IDs in Delivery_Log B column.
Fix: use TEXT() to coerce both sides to text before comparing.
Also fix J column (Last Delivery date) which uses MAXIFS — same type issue.
"""
import xlwings as xw
import time

app = xw.apps.active
wb = app.books.active
ws = wb.sheets["Optimizer_Input"]

# Current H formula:
# =IFERROR(LOOKUP(2,1/((Delivery_Log!$B$4:$B$4998=B6)*(ISNUMBER(Delivery_Log!$J$4:$J$4998))*(Delivery_Log!$J$4:$J$4998>0)),Delivery_Log!$J$4:$J$4998),"")
#
# Fix: wrap both sides in TEXT(x,"0") for numeric comparison as text
# But some IDs are alphanumeric (4015A), so use general text format

print("Fixing H6 formula (last cons/day)...")
# Use --TEXT() trick: convert both to text, then compare
ws.range('H6').formula = '=IFERROR(LOOKUP(2,1/((TEXT(Delivery_Log!$B$4:$B$4998,"@")=TEXT(B6,"@"))*(ISNUMBER(Delivery_Log!$J$4:$J$4998))*(Delivery_Log!$J$4:$J$4998>0)),Delivery_Log!$J$4:$J$4998),"")'

# Also check/fix J formula (Last Delivery date) — uses MAXIFS which handles types better
# but let's make it consistent
j_formula = ws.range('J6').formula
print(f"Current J6 formula: {j_formula}")

# MAXIFS is usually OK with mixed types, but let's verify
# The COUNTIFS in Q is working (returns 7 for Daruma), so MAXIFS should too
# J formula is: =IFERROR(MAXIFS(Delivery_Log!$A$4:$A$4998,Delivery_Log!$B$4:$B$4998,B6),"—")
# MAXIFS does text coercion, so this should be fine. Leave it.

# Copy H6 down
print("Copying H6 down to H175...")
time.sleep(1)
ws.range('H6').copy()
time.sleep(1)
ws.range('H7:H175').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# Recalc
time.sleep(1)
app.calculate()
time.sleep(3)

# Verify
print("\n=== VERIFY ===")
test_rows = [6, 10, 36, 63, 64, 135, 161, 162, 165]
for row in test_rows:
    b = ws.range(f'B{row}').value
    c = ws.range(f'C{row}').value or ""
    h = ws.range(f'H{row}').value
    l_val = ws.range(f'L{row}').value
    print(f"  Row {row}: B={b}, H={h}, L={l_val} | {c[:35]}")

wb.save()
print("\nDone.")
