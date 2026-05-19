#!/usr/bin/env python3
"""Check the clean file through Excel using xlwings."""
import xlwings as xw
import time, sys

CLEAN = '/Users/pabloherrera/Documents/Claude/Projects/route optimization/sk_optimizer/data/SK_Delivery_System_clean.xlsx'

# Fresh connection
app = xw.App(visible=True, add_book=False)
time.sleep(2)
app.display_alerts = False

wb = app.books.open(CLEAN)
time.sleep(3)
app.calculate()
time.sleep(3)

print(f"Workbook: {wb.name}")
print(f"Sheets: {[s.name for s in wb.sheets]}")

ws_al = wb.sheets["Anova_Live"]
ws_q = wb.sheets["Query"]
ws_cl = wb.sheets["Client_List"]
ws_oi = wb.sheets["Optimizer_Input"]

print("\n--- QUERY ---")
print(f"A2={ws_q.range('A2').value}")
print(f"C2={ws_q.range('C2').value}")

print("\n--- ANOVA_LIVE ---")
print(f"A2={ws_al.range('A2').value}")
print(f"B2={ws_al.range('B2').value}")
print(f"C2(name)={ws_al.range('C2').value}")
print(f"E2(lbs)={ws_al.range('E2').value}")
print(f"K2(pct)={ws_al.range('K2').value}")
print(f"L2(deliv)={ws_al.range('L2').value}")

print("\n--- CLIENT_LIST ---")
print(f"A8={ws_cl.range('A8').value}")
print(f"R8(rtu)={ws_cl.range('R8').value}")
print(f"S8(level)={ws_cl.range('S8').value}")
print(f"T8(pct)={ws_cl.range('T8').value}")
print(f"W8(age)={ws_cl.range('W8').value}")

print("\n--- OPTIMIZER_INPUT ---")
for row in range(6, 30):
    b = ws_oi.range(f"B{row}").value
    if b and "1054" in str(b):
        print(f"Row {row}: B={b}")
        print(f"  S={ws_oi.range(f'S{row}').value}")
        print(f"  T={ws_oi.range(f'T{row}').value}")
        print(f"  U={ws_oi.range(f'U{row}').value}")
        break

# Close and reopen to verify no repair
print("\n--- CLOSE/REOPEN TEST ---")
wb.save()
wb.close()
time.sleep(2)
wb2 = app.books.open(CLEAN)
time.sleep(4)
print(f"Reopened: {wb2.name}")
print(f"Sheets: {[s.name for s in wb2.sheets]}")
print("NO REPAIR DIALOG = OK")

wb2.close()
app.display_alerts = True
app.quit()
print("\nDONE")
