#!/usr/bin/env python3
"""
Fix Anova_Live formulas after asset_id was inserted as Query column C.
Old Query: A=device_id, B=customer, C=display_value, D=display_unit, E=scaled, F=scaled_unit, G=product, H=timestamp, I=received_at
New Query: A=device_id, B=customer, C=asset_id, D=display_value, E=display_unit, F=scaled, G=scaled_unit, H=product, I=timestamp, J=received_at
"""
import xlwings as xw
import time

app = xw.apps.active
wb = app.books.active
ws_al = wb.sheets["Anova_Live"]

# Find last device row
a_col = ws_al.range('A2:A120').value
last_row = 1
for i, v in enumerate(a_col):
    if v is not None and str(v).strip() != "":
        last_row = i + 2

print(f"Last device row: {last_row}")

# Anova_Live column mapping (what each column should pull from Query):
# C = Client name (from Client_List, no change)
# D = Customer name from Query col B (no change, still B)
# E = display_value → was Query C, NOW Query D
# F = display_unit → was Query D, NOW Query E
# G = product → was Query G, NOW Query H
# H = timestamp → was Query H, NOW Query I
# I = received_at → was Query I, NOW Query J

print("Fixing Anova_Live formulas for shifted Query columns...")

# C2 stays the same (Client_List lookup)
# D2 stays the same (Query B = customer)
ws_al.range('D2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$B$2:$B$200,""),""))'
# E2: display_value → now Query col D
ws_al.range('E2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$D$2:$D$200,""),""))'
# F2: display_unit → now Query col E
ws_al.range('F2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$E$2:$E$200,""),""))'
# G2: product → now Query col H
ws_al.range('G2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$H$2:$H$200,""),""))'
# H2: timestamp → now Query col I
ws_al.range('H2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$I$2:$I$200,""),""))'
# I2: received_at → now Query col J
ws_al.range('I2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$J$2:$J$200,""),""))'

# J, K, L, M don't reference Query directly (they use Client_List or internal calcs)
# No change needed for those

time.sleep(1)
print(f"Copying D2:I2 down to row {last_row}...")
ws_al.range('D2:I2').copy()
time.sleep(1)
ws_al.range(f'D3:I{last_row}').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# Recalc
time.sleep(1)
app.calculate()
time.sleep(3)

# Verify
print("\n=== VERIFY ===")
for r in [2, 3, 4, 10]:
    a = ws_al.range(f'A{r}').value
    b = ws_al.range(f'B{r}').value
    e = ws_al.range(f'E{r}').value
    g = ws_al.range(f'G{r}').value
    h = ws_al.range(f'H{r}').value
    print(f"  Row {r}: device={a}, client={b}, lbs={e}, product={g}, timestamp={h}")

wb.save()
print("\nDone. Anova_Live formulas updated for new Query column layout.")
