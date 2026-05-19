#!/usr/bin/env python3
"""
Fix Delivery_Log: re-copy C,E-K formulas from row 4 (known good) down to all data rows + buffer.
Problem: formulas are misaligned — row 1480 references B1514 instead of B1480.
Fix: re-fill from a correct source row.
"""
import xlwings as xw
import time

app = xw.apps.active
wb = app.books.active
ws = wb.sheets["Delivery_Log"]

# Find last data row
b_col = ws.range('B4:B5000').value
last_row = 3
for i, v in enumerate(b_col):
    if v is not None and str(v).strip() != "":
        last_row = i + 4

buffer_end = last_row + 300  # room for future entries
print(f"Last data row: {last_row}, filling formulas to row {buffer_end}")

# Verify row 4 is correct
c4 = ws.range('C4').formula
print(f"C4 formula (should ref B4): {c4[:60]}")

# Copy C4:K4 down to cover everything
# But skip column D (Qty Delivered) — that's user-entered data, not a formula
# Check which columns are formulas vs data
print("\nColumn types at row 4:")
for col in ['C','D','E','F','G','H','I','J','K']:
    f = ws.range(f'{col}4').formula
    is_formula = str(f).startswith('=') if f else False
    print(f"  {col}4: {'FORMULA' if is_formula else 'DATA'} = {str(f)[:50] if f else 'empty'}")

# Copy formula columns: C, E, F, G, H, I, J, K (skip D which is qty delivered)
print(f"\nRe-copying C4, E4:K4 formulas down to row {buffer_end}...")

# Do C separately
ws.range('C4').copy()
time.sleep(1)
ws.range(f'C5:C{buffer_end}').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# Do E:K together
ws.range('E4:K4').copy()
time.sleep(1)
ws.range(f'E5:K{buffer_end}').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# Recalc
time.sleep(1)
app.calculate()
time.sleep(3)

# Verify previously-broken rows
print("\n=== VERIFY ===")
for row in [1475, 1480, 1490, 1500, 1508]:
    if row <= last_row:
        b = ws.range(f'B{row}').value
        c = ws.range(f'C{row}').value
        g = ws.range(f'G{row}').value
        h = ws.range(f'H{row}').value
        j = ws.range(f'J{row}').value
        c_formula = ws.range(f'C{row}').formula[:40] if ws.range(f'C{row}').formula else ""
        print(f"  Row {row}: B={b}, C={c}, G={g}, H={h}, J={j} | formula refs: {c_formula}")

# Check an empty-buffer row
er = last_row + 5
c_er = ws.range(f'C{er}').value
print(f"  Row {er} (buffer, no data): C={c_er}")

wb.save()
print("\nDone.")
