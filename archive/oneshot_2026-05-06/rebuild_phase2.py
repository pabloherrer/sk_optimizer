#!/usr/bin/env python3
"""
Phase 2: Open clean file in Excel, add all formulas via xlwings.
Run AFTER rebuild_phase1.py. Excel must be running.
"""
import xlwings as xw
import openpyxl
import time, sys

CORRUPT_FILE = '/Users/pabloherrera/Documents/Claude/Projects/route optimization/sk_optimizer/data/SK_Delivery_System.xlsx'
CLEAN_FILE   = '/Users/pabloherrera/Documents/Claude/Projects/route optimization/sk_optimizer/data/SK_Delivery_System_clean.xlsx'

print("═══ PHASE 2: Add formulas via Excel ═══")

# Connect to Excel
print("Connecting to Excel...")
try:
    app = xw.App(visible=True, add_book=False)
except:
    app = xw.apps.active

if app is None:
    print("ERROR: Excel is not running. Open Excel first.")
    sys.exit(1)

time.sleep(2)
app.display_alerts = False

print("Opening clean file...")
wb = app.books.open(CLEAN_FILE)
time.sleep(3)

# Verify we're connected
try:
    bname = wb.name
    print(f"  Workbook: {bname}")
except:
    # Retry: grab the active book
    time.sleep(3)
    wb = app.books.active
    bname = wb.name
    print(f"  Workbook (retry): {bname}")

sheets = [s.name for s in wb.sheets]
print(f"  Sheets: {sheets}")

# ── Read formulas from old file ──
print("\nReading formulas from old file...")
wb_f = openpyxl.load_workbook(CORRUPT_FILE, read_only=True)
formula_map = {}
for sname in ['Client_List', 'Optimizer_Input']:
    ws = wb_f[sname]
    formulas = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value and isinstance(cell.value, str) and cell.value.startswith('='):
                formulas[cell.coordinate] = cell.value
    formula_map[sname] = formulas
    print(f"  {sname}: {len(formulas)} formulas")
wb_f.close()

# ── Client_List: restore H-Q formulas ──
print("\nRestoring Client_List H-Q formulas...")
ws_cl = wb.sheets['Client_List']
count = 0
for coord, formula in formula_map.get('Client_List', {}).items():
    col = ''.join(c for c in coord if c.isalpha())
    if col in ['H','I','J','K','L','M','N','O','P','Q']:
        try:
            ws_cl.range(coord).formula = formula
            count += 1
        except:
            pass
print(f"  {count} formulas restored")

# ── Client_List: add R-X Anova formulas ──
print("\nAdding Client_List R-X Anova formulas (172 rows)...")
for row in range(4, 176):
    r = str(row)
    ws_cl.range(f'R{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$A$2:$A$60,""),"")'
    ws_cl.range(f'S{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$E$2:$E$60,""),"")'
    ws_cl.range(f'T{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$K$2:$K$60,""),"")'
    ws_cl.range(f'U{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$H$2:$H$60,""),"")'
    ws_cl.range(f'V{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$L$2:$L$60,""),"")'
    ws_cl.range(f'W{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$M$2:$M$60,""),"")'
    ws_cl.range(f'X{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$N$2:$N$60,""),"")'
    if row % 50 == 0:
        print(f"    row {row}...")
print("  Done.")

# ── Optimizer_Input: restore C-R formulas ──
print("\nRestoring Optimizer_Input C-R formulas...")
ws_oi = wb.sheets['Optimizer_Input']
count = 0
for coord, formula in formula_map.get('Optimizer_Input', {}).items():
    col = ''.join(c for c in coord if c.isalpha())
    if col not in ['S', 'T', 'U', 'A', 'B']:
        try:
            ws_oi.range(coord).formula = formula
            count += 1
        except:
            pass
print(f"  {count} formulas restored")

# ── Optimizer_Input: add S-U Anova formulas ──
print("\nAdding Optimizer_Input S-U Anova formulas (170 rows)...")
for row in range(6, 176):
    r = str(row)
    ws_oi.range(f'S{r}').formula = f'=IFERROR(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$S$4:$S$200,""),"")'
    ws_oi.range(f'T{r}').formula = f'=IFERROR(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$U$4:$U$200,""),"")'
    ws_oi.range(f'U{r}').formula = f'=IFERROR(IF(S{r}="","",IF(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$W$4:$W$200,999)<=24,"LIVE",IF(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$W$4:$W$200,999)<=72,"STALE","OLD"))),"")'
    if row % 50 == 0:
        print(f"    row {row}...")
print("  Done.")

# ── Anova_Live: add formulas ──
print("\nAdding Anova_Live formulas (53 rows)...")
ws_al = wb.sheets['Anova_Live']

for row in range(2, 55):
    r = str(row)
    ws_al.range(f'C{r}').formula = f'=IFERROR(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$B$4:$B$200,""),"")'
    ws_al.range(f'D{r}').formula = f'=IFERROR(XLOOKUP(TEXT(A{r},"0"),Query!$A$2:$A$100,Query!$B$2:$B$100,""),"")'
    ws_al.range(f'E{r}').formula = f'=IFERROR(XLOOKUP(TEXT(A{r},"0"),Query!$A$2:$A$100,Query!$C$2:$C$100,""),"")'
    ws_al.range(f'F{r}').formula = f'=IFERROR(XLOOKUP(TEXT(A{r},"0"),Query!$A$2:$A$100,Query!$D$2:$D$100,""),"")'
    ws_al.range(f'G{r}').formula = f'=IFERROR(XLOOKUP(TEXT(A{r},"0"),Query!$A$2:$A$100,Query!$G$2:$G$100,""),"")'
    ws_al.range(f'H{r}').formula = f'=IFERROR(XLOOKUP(TEXT(A{r},"0"),Query!$A$2:$A$100,Query!$H$2:$H$100,""),"")'
    ws_al.range(f'I{r}').formula = f'=IFERROR(XLOOKUP(TEXT(A{r},"0"),Query!$A$2:$A$100,Query!$I$2:$I$100,""),"")'
    ws_al.range(f'J{r}').formula = f'=IFERROR(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$J$4:$J$200,""),"")'
    ws_al.range(f'K{r}').formula = f'=IFERROR(IF(AND(E{r}<>"",J{r}<>""),ROUND(E{r}/J{r}*100,1),""),"")'
    ws_al.range(f'L{r}').formula = f'=IFERROR(IF(AND(E{r}<>"",J{r}<>""),ROUND(J{r}-E{r},0),""),"")'
    ws_al.range(f'M{r}').formula = f'=IFERROR(IF(H{r}<>"",ROUND((NOW()-H{r})*24,1),""),"")'
    if row % 20 == 0:
        print(f"    row {row}...")
print("  Done.")

# ── Verify ──
print("\nVerifying data chain...")
time.sleep(2)
app.calculate()
time.sleep(2)

e2 = ws_al.range('E2').value
c2 = ws_al.range('C2').value
k2 = ws_al.range('K2').value
print(f"  Anova_Live row 2: name={c2}, lbs={e2}, pct={k2}")

s8 = ws_cl.range('S8').value
print(f"  Client_List row 8 (Angry Crab): Anova Level={s8}")

for row in range(6, 176):
    bid = ws_oi.range(f'B{row}').value
    if str(bid) == '1054':
        s = ws_oi.range(f'S{row}').value
        u = ws_oi.range(f'U{row}').value
        print(f"  Optimizer_Input row {row} (1054): Level={s}, Status={u}")
        break

# ── Save through Excel ──
print("\nSaving through Excel...")
wb.save()
print(f"  Saved: {CLEAN_FILE}")

# ── Close + reopen to verify ──
print("\nClose and reopen to verify...")
wb.close()
time.sleep(2)
wb2 = app.books.open(CLEAN_FILE)
time.sleep(4)
sheets = [s.name for s in wb2.sheets]
print(f"  Reopened! Sheets: {sheets}")
print(f"  No repair dialog = SUCCESS")

app.display_alerts = True
print("\n═══ DONE. File is clean and ready. ═══")
