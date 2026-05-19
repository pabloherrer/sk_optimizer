#!/usr/bin/env python3
"""
Fix SK_Delivery_System.xlsx using xlwings (through Excel, no corruption).
Rebuilds Anova_Live, Client_List R-X, and Optimizer_Input S-U formulas.
"""
import xlwings as xw
import time, sys

FILE = '/Users/pabloherrera/Documents/Claude/Projects/route optimization/sk_optimizer/data/SK_Delivery_System.xlsx'

print("Opening Excel...")
app = xw.apps.active
if app is None:
    app = xw.App(visible=True)

# Check if already open
wb = None
for b in app.books:
    if 'SK_Delivery_System' in b.name:
        wb = b
        break

if wb is None:
    wb = app.books.open(FILE)

print(f"Workbook: {wb.name}")
print(f"Sheets: {[s.name for s in wb.sheets]}")

# ── Step 1: Ensure Anova_Live sheet exists ──
sheet_names = [s.name for s in wb.sheets]

if 'Anova_Live' not in sheet_names:
    print("Creating Anova_Live sheet...")
    ws = wb.sheets.add('Anova_Live', after=wb.sheets[-1])
else:
    ws = wb.sheets['Anova_Live']
    print("Anova_Live exists, clearing and rebuilding...")
    ws.clear()

# Headers
headers = ['device_id', 'client_id', 'client_name', 'customer_ref',
           'display_value', 'display_unit', 'product', 'timestamp',
           'received_at', 'tank_capacity', 'pct_full', 'deliverable_lbs',
           'age_hours', 'source']
ws.range('A1').value = headers

# Column widths
col_widths = [14, 10, 35, 35, 14, 12, 16, 22, 22, 14, 10, 14, 10, 8]
for i, w in enumerate(col_widths):
    ws.range((1, i+1)).column_width = w

# Known device-to-client mapping
devices = [
    (124697513, 1054),
    (121381187, 19117),
    (124677515, ''),  # Augustine Casino - not SK client
]

for i, (dev_id, client_id) in enumerate(devices):
    row = i + 2
    ws.range(f'A{row}').value = dev_id
    ws.range(f'B{row}').value = client_id

    # Client name from Client_List
    ws.range(f'C{row}').formula = f'=IFERROR(XLOOKUP(B{row},Client_List!$A$4:$A$200,Client_List!$B$4:$B$200,""),"")'

    # Customer ref from Query (col B = customer)
    ws.range(f'D{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$B$2:$B$100,""),"")'

    # display_value from Query (col C)
    ws.range(f'E{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$C$2:$C$100,""),"")'

    # display_unit from Query (col D)
    ws.range(f'F{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$D$2:$D$100,""),"")'

    # product from Query (col G)
    ws.range(f'G{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$G$2:$G$100,""),"")'

    # timestamp from Query (col H)
    ws.range(f'H{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$H$2:$H$100,""),"")'

    # received_at from Query (col I)
    ws.range(f'I{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$I$2:$I$100,""),"")'

    # tank_capacity from Client_List (col J = tank size)
    ws.range(f'J{row}').formula = f'=IFERROR(XLOOKUP(B{row},Client_List!$A$4:$A$200,Client_List!$J$4:$J$200,""),"")'

    # pct_full
    ws.range(f'K{row}').formula = f'=IFERROR(IF(AND(E{row}<>"",J{row}<>""),ROUND(E{row}/J{row}*100,1),""),"")'

    # deliverable_lbs
    ws.range(f'L{row}').formula = f'=IFERROR(IF(AND(E{row}<>"",J{row}<>""),ROUND(J{row}-E{row},0),""),"")'

    # age_hours
    ws.range(f'M{row}').formula = f'=IFERROR(IF(H{row}<>"",ROUND((NOW()-H{row})*24,1),""),"")'

    # source
    ws.range(f'N{row}').value = 'push'

# Template rows (5-54) with same formulas
print("Adding template rows...")
for row in range(5, 55):
    ws.range(f'C{row}').formula = f'=IFERROR(XLOOKUP(B{row},Client_List!$A$4:$A$200,Client_List!$B$4:$B$200,""),"")'
    ws.range(f'D{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$B$2:$B$100,""),"")'
    ws.range(f'E{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$C$2:$C$100,""),"")'
    ws.range(f'F{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$D$2:$D$100,""),"")'
    ws.range(f'G{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$G$2:$G$100,""),"")'
    ws.range(f'H{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$H$2:$H$100,""),"")'
    ws.range(f'I{row}').formula = f'=IFERROR(XLOOKUP(TEXT(A{row},"0"),Query!$A$2:$A$100,Query!$I$2:$I$100,""),"")'
    ws.range(f'J{row}').formula = f'=IFERROR(XLOOKUP(B{row},Client_List!$A$4:$A$200,Client_List!$J$4:$J$200,""),"")'
    ws.range(f'K{row}').formula = f'=IFERROR(IF(AND(E{row}<>"",J{row}<>""),ROUND(E{row}/J{row}*100,1),""),"")'
    ws.range(f'L{row}').formula = f'=IFERROR(IF(AND(E{row}<>"",J{row}<>""),ROUND(J{row}-E{row},0),""),"")'
    ws.range(f'M{row}').formula = f'=IFERROR(IF(H{row}<>"",ROUND((NOW()-H{row})*24,1),""),"")'

print("Anova_Live done.")

# ── Step 2: Fix Client_List R-X ──
print("Fixing Client_List R-X formulas...")
ws_cl = wb.sheets['Client_List']

# Headers in row 3
cl_headers = ['RTU ID', 'Anova\nLevel (lbs)', 'Anova\n% Full',
              'Anova\nLast Reading', 'Anova\nDeliverable',
              'Anova\nAge (hrs)', 'Anova\nSource']
for i, hdr in enumerate(cl_headers):
    ws_cl.range((3, 18 + i)).value = hdr

# Anova_Live column mapping:
# B=client_id, A=device_id, E=display_value, K=pct_full,
# H=timestamp, L=deliverable, M=age_hours, N=source
for row in range(4, 176):
    r = str(row)
    ws_cl.range(f'R{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$A$2:$A$60,""),"")'
    ws_cl.range(f'S{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$E$2:$E$60,""),"")'
    ws_cl.range(f'T{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$K$2:$K$60,""),"")'
    ws_cl.range(f'U{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$H$2:$H$60,""),"")'
    ws_cl.range(f'V{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$L$2:$L$60,""),"")'
    ws_cl.range(f'W{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$M$2:$M$60,""),"")'
    ws_cl.range(f'X{r}').formula = f'=IFERROR(XLOOKUP(TEXT($A{r},"0"),Anova_Live!$B$2:$B$60,Anova_Live!$N$2:$N$60,""),"")'

print("Client_List R-X done.")

# ── Step 3: Fix Optimizer_Input S-U ──
print("Fixing Optimizer_Input S-U formulas...")
ws_oi = wb.sheets['Optimizer_Input']

# Headers in row 5
ws_oi.range('S5').value = 'Anova Level\n(lbs)'
ws_oi.range('T5').value = 'Anova\nUpdated'
ws_oi.range('U5').value = 'Anova\nStatus'

for row in range(6, 176):
    r = str(row)
    ws_oi.range(f'S{r}').formula = f'=IFERROR(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$S$4:$S$200,""),"")'
    ws_oi.range(f'T{r}').formula = f'=IFERROR(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$U$4:$U$200,""),"")'
    ws_oi.range(f'U{r}').formula = f'=IFERROR(IF(S{r}="","",IF(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$W$4:$W$200,999)<=24,"LIVE",IF(XLOOKUP(B{r},Client_List!$A$4:$A$200,Client_List!$W$4:$W$200,999)<=72,"STALE","OLD"))),"")'

print("Optimizer_Input S-U done.")

# ── Step 4: Verify ──
print("\n=== VERIFICATION ===")

# Check Query sheet
ws_q = wb.sheets['Query']
q_a1 = ws_q.range('A1').value
q_a2 = ws_q.range('A2').value
q_cols = ws_q.range('A1').expand('right').value
print(f"Query headers: {q_cols}")
print(f"Query A2 (device_id): {q_a2}")

# Check Anova_Live
e2 = ws.range('E2').value
c2 = ws.range('C2').value
k2 = ws.range('K2').value
l2 = ws.range('L2').value
print(f"\nAnova_Live row 2:")
print(f"  client_name={c2}")
print(f"  display_value={e2}")
print(f"  pct_full={k2}")
print(f"  deliverable={l2}")

# Check Client_List row 8 (Angry Crab, ID 1054)
r8 = ws_cl.range('R8').value
s8 = ws_cl.range('S8').value
t8 = ws_cl.range('T8').value
w8 = ws_cl.range('W8').value
print(f"\nClient_List row 8 (Angry Crab):")
print(f"  RTU={r8}, Level={s8}, pct={t8}, age_hrs={w8}")

# Check Optimizer_Input for client 1054
# Find the row
for row in range(6, 176):
    bid = ws_oi.range(f'B{row}').value
    if str(bid) == '1054':
        s = ws_oi.range(f'S{row}').value
        t = ws_oi.range(f'T{row}').value
        u = ws_oi.range(f'U{row}').value
        print(f"\nOptimizer_Input row {row} (ID 1054):")
        print(f"  Anova Level={s}, Updated={t}, Status={u}")
        break

# Delete stray sheets
for s in wb.sheets:
    if s.name in ['Sheet1', 'HELPER QUERY CREATOR.']:
        print(f"\nDeleting stray sheet: {s.name}")
        s.delete()

# Save
print("\nSaving...")
wb.save()
print("DONE - file saved through Excel (no corruption)")
