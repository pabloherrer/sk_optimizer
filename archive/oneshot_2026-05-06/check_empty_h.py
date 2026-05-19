#!/usr/bin/env python3
"""Check which rows have empty H (last cons per day) and formulas."""
import xlwings as xw

app = xw.apps.active
wb = app.books.active
ws = wb.sheets["Optimizer_Input"]

print("=== FORMULAS ===")
print(f"H6: {ws.range('H6').formula}")
print(f"H10: {ws.range('H10').formula}")
print(f"L6: {ws.range('L6').formula}")
print(f"N6: {ws.range('N6').formula}")
print()

print("=== ROWS WITH EMPTY H (cons/day) ===")
empty_h = []
for row in range(6, 176):
    b = ws.range(f'B{row}').value
    h = ws.range(f'H{row}').value
    if b and (h is None or h == "" or h == 0):
        c = ws.range(f'C{row}').value or ""
        s = ws.range(f'S{row}').value
        l_est = ws.range(f'L{row}').value
        empty_h.append(row)
        print(f"  Row {row}: {c[:40]:40s} H={h}, S(anova)={s}, L(est)={l_est}")

print(f"\nTotal with empty H: {len(empty_h)}")

# Also check Angry Crab comparison
print("\n=== ANOVA vs ESTIMATE (Angry Crab) ===")
print(f"  H10 (cons/day): {ws.range('H10').value}")
print(f"  L10 (est level): {ws.range('L10').value}")
print(f"  S10 (anova level): {ws.range('S10').value}")
print(f"  Difference: {ws.range('S10').value - ws.range('L10').value if ws.range('S10').value and ws.range('L10').value else 'N/A'}")
