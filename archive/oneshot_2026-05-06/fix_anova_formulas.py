#!/usr/bin/env python3
"""Fix Anova formulas using copy+fill-down for speed."""
import xlwings as xw
import time

app = xw.apps.active
wb = app.books.active
print(f"Workbook: {wb.name}")

ws_cl = wb.sheets["Client_List"]
ws_oi = wb.sheets["Optimizer_Input"]

# ── Client_List R-X: set row 4, then fill down to 175 ──
print("Setting Client_List R4:X4 formulas...")
ws_cl.range('R4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$A$2:$A$60,""),"")'
ws_cl.range('S4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$E$2:$E$60,""),"")'
ws_cl.range('T4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$K$2:$K$60,""),"")'
ws_cl.range('U4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$H$2:$H$60,""),"")'
ws_cl.range('V4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$L$2:$L$60,""),"")'
ws_cl.range('W4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$M$2:$M$60,""),"")'
ws_cl.range('X4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$60,Anova_Live!$N$2:$N$60,""),"")'

print("Copying R4:X4 down to row 175...")
ws_cl.range('R4:X4').copy()
time.sleep(1)
ws_cl.range('R5:X175').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False
print("  Client_List R-X done.")

# ── Optimizer_Input S-U: set row 6, then fill down to 175 ──
print("Setting Optimizer_Input S6:U6 formulas...")
ws_oi.range('S6').formula = '=IFERROR(XLOOKUP(B6,Client_List!$A$4:$A$200,Client_List!$S$4:$S$200,""),"")'
ws_oi.range('T6').formula = '=IFERROR(XLOOKUP(B6,Client_List!$A$4:$A$200,Client_List!$U$4:$U$200,""),"")'
ws_oi.range('U6').formula = '=IFERROR(IF(S6="","",IF(XLOOKUP(B6,Client_List!$A$4:$A$200,Client_List!$W$4:$W$200,999)<=24,"LIVE",IF(XLOOKUP(B6,Client_List!$A$4:$A$200,Client_List!$W$4:$W$200,999)<=72,"STALE","OLD"))),"")'

print("Copying S6:U6 down to row 175...")
ws_oi.range('S6:U6').copy()
time.sleep(1)
ws_oi.range('S7:U175').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False
print("  Optimizer_Input S-U done.")

# Recalc and verify
time.sleep(1)
app.calculate()
time.sleep(3)

print("\n=== VERIFY ===")
print(f"Client_List R8(RTU): {ws_cl.range('R8').value}")
print(f"Client_List S8(level): {ws_cl.range('S8').value}")
print(f"Client_List T8(pct): {ws_cl.range('T8').value}")

for row in range(6, 30):
    b = ws_oi.range(f"B{row}").value
    if b and "1054" in str(b):
        print(f"Opt_Input row {row}: S={ws_oi.range(f'S{row}').value}, U={ws_oi.range(f'U{row}').value}")
        break

wb.save()
print("\nSaved.")
