#!/usr/bin/env python3
"""
Modify Optimizer_Input L column (Est. Current) to use Anova level when LIVE.
Logic: IF Anova status is LIVE and has data → use Anova level (S)
       ELSE → fall back to delivery-based estimate (G - K*H)

This means M (Refill), N (Days Until Empty), O (Next Delivery By)
all automatically use the best available data.
"""
import xlwings as xw
import time

app = xw.apps.active
wb = app.books.active
ws = wb.sheets["Optimizer_Input"]

# Current L formula:
# =IFERROR(IF(OR(K6="",H6=""),"",MAX(G6*0.03,G6-K6*H6)),"")
#
# New L formula: prioritize Anova when LIVE
# =IF(AND(U6="LIVE",S6<>""), S6, IFERROR(IF(OR(K6="",H6=""),"",MAX(G6*0.03,G6-K6*H6)),""))

print("Setting new L6 formula (Anova-first level)...")
ws.range('L6').formula = '=IF(AND(U6="LIVE",S6<>""),S6,IFERROR(IF(OR(K6="",H6=""),"",MAX(G6*0.03,G6-K6*H6)),""))'

# Copy down
print("Copying L6 down to L175...")
time.sleep(1)
ws.range('L6').copy()
time.sleep(1)
ws.range('L7:L175').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# Recalc
time.sleep(1)
app.calculate()
time.sleep(3)

# Verify
print("\n=== VERIFY ===")
print("Clients with LIVE Anova data:")
for row in range(6, 176):
    u = ws.range(f'U{row}').value
    if u == "LIVE":
        b = ws.range(f'B{row}').value
        c = ws.range(f'C{row}').value or ""
        s = ws.range(f'S{row}').value
        l_val = ws.range(f'L{row}').value
        m_val = ws.range(f'M{row}').value
        n_val = ws.range(f'N{row}').value
        s_str = f"{s:.1f}" if s else "—"
        l_str = f"{l_val:.1f}" if l_val else "—"
        m_str = f"{m_val:.1f}" if m_val else "—"
        n_str = f"{n_val:.1f}" if n_val else "—"
        print(f"  Row {row}: {c[:35]:35s} S={s_str}, L={l_str}, M={m_str}, N={n_str}")

# Also show a non-LIVE client to confirm fallback works
print("\nNon-LIVE fallback example:")
for row in range(6, 30):
    u = ws.range(f'U{row}').value
    h = ws.range(f'H{row}').value
    if u != "LIVE" and h:
        c = ws.range(f'C{row}').value or ""
        l_val = ws.range(f'L{row}').value
        print(f"  Row {row}: {c[:35]:35s} L(estimate)={l_val}")
        break

wb.save()
print("\nDone. L column now uses Anova level when LIVE, estimate otherwise.")
