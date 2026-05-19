#!/usr/bin/env python3
"""
Populate Anova_Live with ALL devices from datachannel CSV.
Manual override mapping for tricky name mismatches.
"""
import xlwings as xw
import csv, time, re

CSV_PATH = '/Users/pabloherrera/Documents/Claude/Projects/route optimization/sk_optimizer/data/datachannel-list.csv'

# ── Manual override: Anova customer name → Client_List client_id ──
# Built by comparing Anova names to Client_List naming convention
MANUAL_MAP = {
    "Back Yard Gilbert": 19039,
    "Backyard desert ridge": 2067,
    "Booty's Goodyear": 2023,
    "Booty's surprise": 2016,
    "HERMOSA INN": 20033,
    "Jays Travel Center": 10012,
    "Jinky Jakes": 10011,
    "K O'Donnell's sport bar & grill": 11013,
    "LITTLE'S O'S Central": 12055,
    "Long Wong's  75ave/lower buckeye": 12051,
    "Long Wongs 35th ave": 12050,
    "MANUALS SHEA BLVD / PHX": 13069,
    "MANUEL'S CHANDLER": 13057,
    "Manuel's / peoria": 13013,
    "Manuel's Bell": 13009,
    "OREGANO'S SURPRISE": 15016,
    "OREGANOS BELL ROAD": 15003,
    "Oregano's Pima": 15041,
    "Oregano's paradise valley": 15026,
    "Oreganos Camelback": 15020,
    "Oreganos Chandler": 15027,
    "Oreganos Elliot": 15025,
    "Oreganos Goodyear": 15005,
    "Oreganos Mesa": 15023,
    "Oreganos scottsdale": 15018,
    "Tailgaters Suprise": 20091,
    "Tailgaters WATSON": 20094,
    "Tailgaters cave creek": 20095,
    "Tailgaters peoria / lake pleasant pkwy": 20090,
    "Tailgaters prescott": 20089,
    "VIg peoria/ park west": 20097,
    "sugar jam": 19077,
    "Dillons Western Trails": 4025,
    "State Farm Stadium": 3063,
    "Il primo Pizza & wings": 20098,
    "Baja Joe MESA": 2053,
    "VIG SCOTTSDALE": 20013,  # THE VIG HAYDEN (closest Scottsdale Vig)
    "Crown Plaza Resort": 3064,
    "Cowboy Cookin": 3028,
    # These are NOT in Client_List (out-of-state / non-delivery):
    # Augustine Casino, Fire Rock Casino, Firerock Country Club,
    # Flowing Water Casino, Harrah Rincon Casino, Morongo Casino,
    # Morongo Bowling Alley, Morongo Pit-Stop Restaurant,
    # Northern Edge casino, Pauma casino, Soboba Casino,
    # Cafe Valley Bakery, Cardenas mkt/Ranch market,
    # Mama Lolas Tortillas, SANTA FE TORTILLA NEW MEXICO,
    # Legacy Foods, Upper Crust, Frites Street,
    # Oreganos Mesa/signal Butte, Tailgaters Goodyear arizona,
    # Tailgaters litchfield park
    "Roadrunner Restaurant": 18036,
    "Popo's Bell road": 16015,
    "Popo's Goodyear": 16051,  # closest: POPOS MCDOWELL (user can correct)
    "La Canasta Tortilla": None,  # not in Client_List
}

# ── Step 1: Parse CSV ──
print("Step 1: Reading CSV...")
with open(CSV_PATH, encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))

devices = {}
for r in rows:
    if r['Type'] != 'Level':
        continue
    rtu = r['RTU'].strip()
    ch = r['RTU Channel'].strip()
    key = (rtu, ch)
    if key not in devices:
        devices[key] = {'rtu': rtu, 'channel': ch, 'customer': r['Customer'].strip(), 'product': r['Product'].strip()}

print(f"  {len(devices)} Level channels")

# ── Step 2: Read Client_List ──
print("Step 2: Reading Client_List...")
app = xw.apps.active
wb = app.books.active
ws_cl = wb.sheets["Client_List"]

cl_data = ws_cl.range('A4:B175').value
client_short = {}
for row in cl_data:
    if row[0] and row[1]:
        cid = int(row[0]) if isinstance(row[0], float) else row[0]
        full = str(row[1]).strip()
        m = re.match(r'^[A-Za-z0-9]+ - \S+ - (.+)$', full)
        if m:
            short = m.group(1).strip().lower()
            client_short[short] = cid

print(f"  {len(client_short)} clients with short names")

# ── Step 3: Match ──
def norm(s):
    s = s.lower().strip()
    s = re.sub(r"[''`']", "", s)  # remove apostrophes only
    s = re.sub(r"[/,&]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

norm_short = {norm(k): v for k, v in client_short.items()}

device_list = []
matched = 0
unmatched = []

for key, dev in sorted(devices.items()):
    cust = dev['customer']

    # 1) Manual override
    client_id = MANUAL_MAP.get(cust)

    # 2) Auto match on normalized short name
    if not client_id:
        nc = norm(cust)
        if nc in norm_short:
            client_id = norm_short[nc]

    # 3) Substring
    if not client_id:
        nc = norm(cust)
        for ns, cid in norm_short.items():
            if nc in ns or ns in nc:
                client_id = cid
                break

    # 4) First 2 words
    if not client_id:
        nc = norm(cust)
        words = nc.split()
        if len(words) >= 2:
            prefix2 = " ".join(words[:2])
            for ns, cid in norm_short.items():
                if ns.startswith(prefix2):
                    client_id = cid
                    break

    if client_id:
        matched += 1
    else:
        unmatched.append(cust)

    rtu = dev['rtu']
    try:
        rtu_val = int(rtu)
    except ValueError:
        rtu_val = rtu

    device_list.append([rtu_val, client_id or ''])

print(f"  Matched: {matched}/{len(device_list)}")
if unmatched:
    print(f"  Unmatched ({len(unmatched)}) — not in Client_List:")
    for u in sorted(unmatched):
        print(f"    - {u}")

# ── Step 4: Write to Anova_Live ──
print(f"\nStep 4: Writing {len(device_list)} devices...")
ws_al = wb.sheets["Anova_Live"]

ws_al.range('A2:N120').clear_contents()
time.sleep(1)

last_row = len(device_list) + 1
ws_al.range(f'A2:B{last_row}').value = device_list
print(f"  Bulk wrote A2:B{last_row}")

# ── Step 5: Formulas with IF guards ──
print("Step 5: Setting formulas...")
ws_al.range('C2').formula = '=IF(B2="","",IFERROR(XLOOKUP(B2,Client_List!$A$4:$A$200,Client_List!$B$4:$B$200,""),""))'
ws_al.range('D2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$B$2:$B$200,""),""))'
ws_al.range('E2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$C$2:$C$200,""),""))'
ws_al.range('F2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$D$2:$D$200,""),""))'
ws_al.range('G2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$G$2:$G$200,""),""))'
ws_al.range('H2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$H$2:$H$200,""),""))'
ws_al.range('I2').formula = '=IF(A2="","",IFERROR(XLOOKUP(A2,Query!$A$2:$A$200,Query!$I$2:$I$200,""),""))'
ws_al.range('J2').formula = '=IF(B2="","",IFERROR(XLOOKUP(B2,Client_List!$A$4:$A$200,Client_List!$J$4:$J$200,""),""))'
ws_al.range('K2').formula = '=IF(OR(E2="",J2=""),"",IFERROR(ROUND(E2/J2*100,1),""))'
ws_al.range('L2').formula = '=IF(OR(E2="",J2=""),"",IFERROR(ROUND(J2-E2,0),""))'
ws_al.range('M2').formula = '=IF(H2="","",IFERROR(ROUND((NOW()-H2)*24,1),""))'
time.sleep(1)

print(f"  Copying C2:M2 down to row {last_row}...")
ws_al.range('C2:M2').copy()
time.sleep(1)
ws_al.range(f'C3:M{last_row}').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# ── Step 6: Expand Client_List R-X range ──
print("Step 6: Updating Client_List R-X...")
ws_cl.range('R4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$A$2:$A$120,""),"")'
ws_cl.range('S4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$E$2:$E$120,""),"")'
ws_cl.range('T4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$K$2:$K$120,""),"")'
ws_cl.range('U4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$H$2:$H$120,""),"")'
ws_cl.range('V4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$L$2:$L$120,""),"")'
ws_cl.range('W4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$M$2:$M$120,""),"")'
ws_cl.range('X4').formula = '=IFERROR(XLOOKUP($A4,Anova_Live!$B$2:$B$120,Anova_Live!$N$2:$N$120,""),"")'
time.sleep(1)
ws_cl.range('R4:X4').copy()
time.sleep(1)
ws_cl.range('R5:X175').paste(paste='formulas')
time.sleep(1)
app.api.cut_copy_mode = False

# ── Step 7: Recalc + verify ──
time.sleep(1)
app.calculate()
time.sleep(3)

print("\n=== VERIFY ===")
for r in [2, 3, 4, 10, 50]:
    a = ws_al.range(f'A{r}').value
    b = ws_al.range(f'B{r}').value
    c = ws_al.range(f'C{r}').value
    e = ws_al.range(f'E{r}').value
    m = ws_al.range(f'M{r}').value
    print(f"  Row {r}: A={a}, B={b}, C={c}, E={e}, M={m}")

er = last_row + 1
print(f"  Row {er} (empty): A={ws_al.range(f'A{er}').value}, C={ws_al.range(f'C{er}').value}")

print(f"\n  Client_List S8: {ws_cl.range('S8').value}")

wb.save()
print(f"\nDone. {len(device_list)} devices, {matched} matched to client_ids.")
