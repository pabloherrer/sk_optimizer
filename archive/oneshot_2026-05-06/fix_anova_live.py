#!/usr/bin/env python3
"""Fix Anova_Live formulas: remove TEXT() wrapping, use direct number match."""
import xlwings as xw
import time

app = xw.apps.active
wb = app.books.active
ws = wb.sheets["Anova_Live"]

# Set row 2 formulas (template), then copy down
print("Setting Anova_Live row 2 formulas...")
ws.range('C2').formula = '=IFERROR(XLOOKUP(B2,Client_List!$A$4:$A$200,Client_List!$B$4:$B$200,""),"")'
ws.range('D2').formula = '=IFERROR(XLOOKUP(A2,Query!$A$2:$A$100,Query!$B$2:$B$100,""),"")'
ws.range('E2').formula = '=IFERROR(XLOOKUP(A2,Query!$A$2:$A$100,Query!$C$2:$C$100,""),"")'
ws.range('F2').formula = '=IFERROR(XLOOKUP(A2,Query!$A$2:$A$100,Query!$D$2:$D$100,""),"")'
ws.range('G2').formula = '=IFERROR(XLOOKUP(A2,Query!$A$2:$A$100,Query!$G$2:$G$100,""),"")'
ws.range('H2').formula = '=IFERROR(XLOOKUP(A2,Query!$A$2:$A$100,Query!$H$2:$H$100,""),"")'
ws.range('I2').formula = '=IFERROR(XLOOKUP(A2,Query!$A$2:$A$100,Query!$I$2:$I$100,""),"")'
ws.range('J2').formula = '=IFERROR(XLOOKUP(B2,Client_List!$A$4:$A$200,Client_List!$J$4:$J$200,""),"")'
ws.range('K2').formula = '=IFERROR(IF(AND(E2<>"",J2<>""),ROUND(E2/J2*100,1),""),"")'
ws.range('L2').formula = '=IFERROR(IF(AND(E2<>"",J2<>""),ROUND(J2-E2,0),""),"")'
ws.range('M2').formula = '=IFERROR(IF(H2<>"",ROUND((NOW()-H2)*24,1),""),"")'

print("Copying C2:M2 down to row 54...")
ws.range('C2:M2').copy()
time.sleep(1)
ws.range('C3:M54').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# Clean up test cells from earlier
for cell in ['P2','P3','P4','P5','Q2']:
    ws.range(cell).clear_contents()

time.sleep(1)
app.calculate()
time.sleep(3)

print("\n=== VERIFY ===")
print(f"E2(lbs): {ws.range('E2').value}")
print(f"C2(name): {ws.range('C2').value}")
print(f"K2(pct): {ws.range('K2').value}")
print(f"L2(deliv): {ws.range('L2').value}")
print(f"M2(age_hrs): {ws.range('M2').value}")
print(f"H2(timestamp): {ws.range('H2').value}")

# Check downstream
ws_cl = wb.sheets["Client_List"]
ws_oi = wb.sheets["Optimizer_Input"]
print(f"\nClient_List S8: {ws_cl.range('S8').value}")
print(f"Client_List W8(age): {ws_cl.range('W8').value}")

for row in range(6, 30):
    b = ws_oi.range(f"B{row}").value
    if b and "1054" in str(b):
        print(f"Opt_Input row {row}: S={ws_oi.range(f'S{row}').value}, U={ws_oi.range(f'U{row}').value}")
        break

wb.save()
print("\nSaved.")
