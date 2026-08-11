"""
Bidfood Stock Engine  — Part C
================================
Loads Bidfood order data, maps sites via Drop Account Number,
parses Pack Sizes to total gram/ml quantities, and produces
a stock report + compliance gap vs Ingredient Requirements.

Inputs:
    processed_data (27).xlsx          (Bidfood order export — update filename as needed)
    Bidfood Drop Account Mapping.xlsx  (site mapping)
    Ingredient_Requirements_Report.xlsx (from ingredient_requirements_engine.py)

Output:
    Bidfood_Stock_Report.xlsx
"""

import os, sys, re, warnings
warnings.filterwarnings("ignore")
import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))

BIDFOOD_FILE  = os.path.join(DIR, "processed_data (27).xlsx")
MAPPING_FILE  = os.path.join(DIR, "Bidfood Drop Account Mapping.xlsx")
ING_REQ_FILE  = os.path.join(DIR, "Ingredient_Requirements_Report.xlsx")
OUT_FILE      = "/tmp/Bidfood_Stock_Report.xlsx"
FINAL_FILE    = os.path.join(DIR, "Bidfood_Stock_Report.xlsx")

for f in [BIDFOOD_FILE, MAPPING_FILE, ING_REQ_FILE]:
    if not os.path.exists(f):
        sys.exit(f"ERROR: file not found: {f}")

# ── [1/5] Load Bidfood orders ────────────────────────────────────────────────
print("[1/5] Loading Bidfood orders ...")
bf = pd.read_excel(BIDFOOD_FILE, dtype=str, engine="calamine")
bf.columns = bf.columns.str.strip()
bf["Customer Code"]      = bf["Customer Code"].fillna("").str.strip()
bf["Product Code"]       = bf["Product Code"].fillna("").str.strip()
bf["Description"]        = bf["Description"].fillna("").str.strip()
bf["Brand"]              = bf["Brand"].fillna("").str.strip()
bf["Pack Size"]          = bf["Pack Size"].fillna("").str.strip()
bf["Unit of Measure"]    = bf["Unit of Measure"].fillna("").str.strip()
bf["Quantity Ordered"]   = pd.to_numeric(bf["Quantity Ordered"], errors="coerce").fillna(0)
bf["Unit Price"]         = pd.to_numeric(bf["Unit Price"],       errors="coerce").fillna(0)
bf["Total"]              = pd.to_numeric(bf["Total"],            errors="coerce").fillna(0)
bf["Order Date"]         = bf["Order Date"].fillna("").str.strip()
bf["Delivery Date"]      = bf["Delivery Date"].fillna("").str.strip()
bf["Account Name"]       = bf["Account Name"].fillna("").str.strip()
print(f"  Total rows: {len(bf):,}")

# ── [2/5] Map site via Drop Account Number ───────────────────────────────────
print("\n[2/5] Mapping sites ...")
mp = pd.read_excel(MAPPING_FILE, sheet_name="Site Mapping", dtype=str, engine="calamine")
mp.columns = mp.columns.str.strip()
mp["Drop Account Number - Bidfood"] = mp["Drop Account Number - Bidfood"].fillna("").str.strip()
mp["Site Key"]   = mp["Site Key"].fillna("").str.strip()
mp["Store Name"] = mp["Store Name"].fillna("").str.strip()

acc_to_site = dict(zip(mp["Drop Account Number - Bidfood"], mp["Site Key"]))
acc_to_store = dict(zip(mp["Drop Account Number - Bidfood"], mp["Store Name"]))

bf["Site_Key"]   = bf["Customer Code"].map(acc_to_site).fillna("")
bf["Store_Name"] = bf["Customer Code"].map(acc_to_store).fillna("")

matched_mask = bf["Site_Key"] != ""
print(f"  Matched to LKN site:  {matched_mask.sum():,} rows")
print(f"  Unmatched (non-LKN):  {(~matched_mask).sum():,} rows")

bf_lkn  = bf[matched_mask].copy()
bf_other = bf[~matched_mask].copy()

# ── [3/5] Parse Pack Size ────────────────────────────────────────────────────
print("\n[3/5] Parsing Pack Sizes ...")

def parse_pack_size(raw):
    """
    Returns (qty_per_case, uom) where uom is 'g', 'ml', or 'each'.
    Handles formats: 6-1kg, 4-2.5kg, 24-330ml, 4-30x56g, 1-ea, 60-65g, etc.
    """
    if not raw or str(raw).strip() == "":
        return None, None
    s = str(raw).strip().lower().replace(" ", "")

    # Three-part: {N}-{A}x{B}{unit}  e.g. '4-30x56g'
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(kg|g|ltr|l|ml)?$", s)
    if m:
        outer, inner, amount = float(m.group(1)), float(m.group(2)), float(m.group(3))
        unit = m.group(4) or "g"
        total = outer * inner * amount
        if unit == "kg":          return total * 1000, "g"
        elif unit in ("ltr","l"): return total * 1000, "ml"
        elif unit == "ml":        return total, "ml"
        else:                     return total, "g"

    # Three-part no unit: {N}-{A}x{B}  e.g. '1-48 x 75'
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$", s)
    if m:
        outer, inner, amount = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return outer * inner * amount, "g"

    # Weight range: {N}-{A}-{B}{unit}  e.g. '20-140-170'
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)(g|kg|ml)?$", s)
    if m:
        count = float(m.group(1))
        lo, hi = float(m.group(2)), float(m.group(3))
        unit = m.group(4) or "g"
        mid = (lo + hi) / 2
        total = count * mid
        if unit == "kg":          return total * 1000, "g"
        elif unit == "ml":        return total, "ml"
        else:                     return total, "g"

    # Two-part: {N}-{A}{unit} or {N}x{A}{unit}
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)(kg|g|ltr|l|ml|gpk|ea|each|bun|pk|ptn|pt|box|ca|roll|m)?$",
                 s, re.I)
    if m:
        count, amount = float(m.group(1)), float(m.group(2))
        unit = (m.group(3) or "g").lower()
        if unit == "kg":              return count * amount * 1000, "g"
        elif unit in ("ltr","l"):     return count * amount * 1000, "ml"
        elif unit == "ml":            return count * amount, "ml"
        elif unit in ("g","gpk"):     return count * amount, "g"
        else:                         return count * amount, "each"

    # Edge: '1-4-5kg' → 4.5 kg
    m = re.match(r"^(\d+)-(\d+)-(\d+)(kg|g|ltr|l|ml)?$", s)
    if m:
        outer = float(m.group(1))
        lo, hi = float(m.group(2)), float(m.group(3))
        unit = m.group(4) or "g"
        mid = (lo + hi) / 2
        total = outer * mid
        if unit == "kg":          return total * 1000, "g"
        elif unit in ("ltr","l"): return total * 1000, "ml"
        elif unit == "ml":        return total, "ml"
        else:                     return total, "g"

    return None, None

bf_lkn[["Pack_Qty","Pack_UOM"]] = pd.DataFrame(
    bf_lkn["Pack Size"].apply(parse_pack_size).tolist(),
    index=bf_lkn.index
)
bf_lkn["Pack_Qty"] = pd.to_numeric(bf_lkn["Pack_Qty"], errors="coerce")

parsed_ok = bf_lkn["Pack_Qty"].notna().sum()
print(f"  Pack sizes parsed: {parsed_ok:,}/{len(bf_lkn):,} "
      f"({parsed_ok/len(bf_lkn)*100:.1f}%)")

unparsed = bf_lkn[bf_lkn["Pack_Qty"].isna()]["Pack Size"].unique()
if len(unparsed):
    print(f"  Unparsed formats ({len(unparsed)}): {unparsed.tolist()}")

# Total ordered quantity = cases ordered × qty per case
bf_lkn["Total_Ordered_Qty"] = bf_lkn["Quantity Ordered"] * bf_lkn["Pack_Qty"]
bf_lkn["Total_Spend_GBP"]   = bf_lkn["Total"]

# ── [4/5] Aggregate site stock ───────────────────────────────────────────────
print("\n[4/5] Aggregating stock by site ...")

site_stock = (bf_lkn
    .groupby(["Site_Key","Store_Name","Product Code","Description","Brand",
              "Pack Size","Pack_UOM","Unit of Measure"], dropna=False)
    .agg(
        Cases_Ordered   =("Quantity Ordered","sum"),
        Total_Ordered_Qty=("Total_Ordered_Qty","sum"),
        Total_Spend_GBP =("Total_Spend_GBP","sum"),
    )
    .reset_index()
    .sort_values(["Site_Key","Product Code"]))

sku_summary = (bf_lkn
    .groupby(["Product Code","Description","Brand","Pack Size","Pack_UOM"], dropna=False)
    .agg(
        Cases_Ordered    =("Quantity Ordered","sum"),
        Total_Ordered_Qty=("Total_Ordered_Qty","sum"),
        Total_Spend_GBP  =("Total_Spend_GBP","sum"),
        Sites_Ordering   =("Site_Key","nunique"),
    )
    .reset_index()
    .sort_values("Total_Ordered_Qty", ascending=False))

print(f"  Site stock rows: {len(site_stock):,}")
print(f"  Unique SKUs:     {sku_summary['Product Code'].nunique():,}")
print(f"  Unique sites:    {site_stock['Site_Key'].nunique():,}")

# ── [5a] Compliance gap: join with Ingredient Requirements ───────────────────
print("\n[5/5] Computing compliance gap ...")

ing_req = pd.read_excel(ING_REQ_FILE, sheet_name="Site Raw Material",
                        dtype=str, engine="calamine")
ing_req.columns = ing_req.columns.str.strip()
ing_req["Total_Raw_Qty"] = pd.to_numeric(ing_req["Total_Raw_Qty"], errors="coerce").fillna(0)

# ing_req uses Store (Site Key) and SKU columns
# Bidfood uses Site_Key and Product Code
# Join on Site_Key = Store  AND  Product Code = SKU
req_by_site = (ing_req
    .groupby(["Store","SKU","Ingredient","UOM"], dropna=False)
    ["Total_Raw_Qty"].sum()
    .reset_index()
    .rename(columns={"Store":"Site_Key","SKU":"Product Code","Total_Raw_Qty":"Required_Qty","UOM":"Req_UOM"}))

# Bidfood stock aggregated to Site + Product Code
stock_by_site = (bf_lkn
    .groupby(["Site_Key","Product Code","Description","Pack_UOM"], dropna=False)
    .agg(Total_Ordered_Qty=("Total_Ordered_Qty","sum"))
    .reset_index()
    .rename(columns={"Pack_UOM":"Ord_UOM"}))

# Merge: only rows where Product Code exists in both (43 SKUs overlap)
compliance = req_by_site.merge(stock_by_site, on=["Site_Key","Product Code"], how="outer")
compliance["Required_Qty"]     = compliance["Required_Qty"].fillna(0)
compliance["Total_Ordered_Qty"]= compliance["Total_Ordered_Qty"].fillna(0)
compliance["Gap"]              = compliance["Total_Ordered_Qty"] - compliance["Required_Qty"]
compliance["Status"] = compliance["Gap"].apply(
    lambda g: "Surplus" if g > 0 else ("Deficit" if g < 0 else "Exact"))
compliance = compliance.sort_values(["Site_Key","Product Code"])

print(f"  Compliance rows: {len(compliance):,}")
print(f"  Status: {compliance['Status'].value_counts().to_dict()}")

# ── Write output ─────────────────────────────────────────────────────────────
import xlsxwriter, shutil

def _clean(v):
    if v is None: return ""
    if isinstance(v, float) and pd.isna(v): return ""
    return v

xwb = xlsxwriter.Workbook(OUT_FILE, {"strings_to_numbers": False, "nan_inf_to_errors": True})
fmt_cache = {}

def xfmt(bg=None, bold=False, size=10, color="000000", align="left", border=True):
    key = (bg, bold, size, color, align, border)
    if key not in fmt_cache:
        f = xwb.add_format({"font_name":"Calibri","font_size":size,"font_color":color,
                             "bold":bold,"align":align,"valign":"vcenter"})
        if bg: f.set_bg_color(bg)
        if border: f.set_border(1); f.set_border_color("#D0D0D0")
        fmt_cache[key] = f
    return fmt_cache[key]

HDR   = xfmt(bg="#1F3864", bold=True, color="FFFFFF", align="center")
PLAIN = xfmt()
RED   = xfmt(bg="#FADBD8")
GRN   = xfmt(bg="#D5F5E3")
YLW   = xfmt(bg="#FEF9E7")

def write_sheet(ws, df, col_widths, row_color_fn=None, default_fmt=None):
    cols = list(df.columns)
    for ci, col in enumerate(cols): ws.write(0, ci, col, HDR)
    ws.set_row(0, 26); ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols)-1)
    for ci, w in enumerate(col_widths): ws.set_column(ci, ci, w)
    if row_color_fn is None:
        rfmt = default_fmt or PLAIN
        for ci, col in enumerate(cols):
            ws.write_column(1, ci, [_clean(v) for v in df[col]], rfmt)
    else:
        for ri, row in enumerate(df.itertuples(index=False), start=1):
            rfmt = row_color_fn(row)
            ws.write_row(ri, 0, [_clean(v) for v in row], rfmt)

# Dashboard
ws_dash = xwb.add_worksheet("Dashboard")
ws_dash.set_column(0, 0, 42); ws_dash.set_column(1, 1, 22)
b14 = xfmt(bold=True, size=14, border=False)
b12 = xfmt(bold=True, size=12, border=False)
n11 = xfmt(size=11, border=False)
b11 = xfmt(bold=True, size=11, border=False)

ws_dash.write(0, 0, "Bidfood Stock Report", b14)
ws_dash.write(1, 0, f"Source: {os.path.basename(BIDFOOD_FILE)}", n11)
r = 3
ws_dash.write(r, 0, "Summary", b12); r += 1
for lbl, val in [
    ("Total order rows (all accounts)",    int(len(bf))),
    ("LKN partner rows (mapped to site)", int(len(bf_lkn))),
    ("Unique LKN sites",                  int(site_stock["Site_Key"].nunique())),
    ("Unique product codes (SKUs)",        int(sku_summary["Product Code"].nunique())),
    ("Total LKN spend (GBP)",             round(bf_lkn["Total"].astype(float).sum(), 2)),
    ("SKUs matching ingredient requirements", int(compliance[compliance["Required_Qty"]>0]["Product Code"].nunique())),
]:
    ws_dash.write(r, 0, lbl, n11); ws_dash.write(r, 1, val, b11); r += 1

# Site Stock
ws_site = xwb.add_worksheet("Site Stock")
write_sheet(ws_site, site_stock,
    [28, 38, 14, 44, 20, 12, 8, 8, 14, 18, 14])

# SKU Summary
ws_sku = xwb.add_worksheet("SKU Summary")
write_sheet(ws_sku, sku_summary,
    [14, 44, 20, 12, 8, 14, 18, 14, 12])

# Compliance Gap
def _gap_color(row):
    s = getattr(row, "Status", "")
    if s == "Surplus": return GRN
    if s == "Deficit": return RED
    return YLW

ws_comp = xwb.add_worksheet("Compliance Gap")
write_sheet(ws_comp, compliance,
    [28, 14, 44, 8, 18, 38, 8, 18, 16, 14, 12],
    row_color_fn=_gap_color)

# Unmatched accounts
ws_um = xwb.add_worksheet("Unmatched Accounts")
um_sum = (bf_other.groupby(["Customer Code","Account Name"])
    .agg(Rows=("Product Code","count"), Spend=("Total","sum"))
    .reset_index())
um_sum["Spend"] = pd.to_numeric(um_sum["Spend"], errors="coerce").fillna(0)
write_sheet(ws_um, um_sum, [18, 42, 10, 14], default_fmt=xfmt(bg="#FEF9E7"))

xwb.close()
shutil.copy2(OUT_FILE, FINAL_FILE)
print(f"\nOK Saved -> {FINAL_FILE}")
print(f"\nResults:")
print(f"  LKN sites in stock data:  {site_stock['Site_Key'].nunique():,}")
print(f"  Unique SKUs ordered:       {sku_summary['Product Code'].nunique():,}")
print(f"  Compliance rows:           {len(compliance):,}")
print(f"  Surplus / Deficit / Exact: {compliance['Status'].value_counts().to_dict()}")
print(f"  Total LKN spend:           GBP {bf_lkn['Total'].astype(float).sum():,.2f}")
