"""
Ingredient Requirements Engine  — Part B
=========================================
Joins site-level matched sales against Recipe Builder ingredient lists
to compute how much of each raw material each site required.

Inputs (same folder):
    Raw_Material_Usage_Output.xlsx      (from recipe_usage_engine.py)
    PLU_Mapping_Complete.xlsx
    Recipe builder.xlsx

Output:
    Ingredient_Requirements_Report.xlsx
"""

import os, sys, warnings
warnings.filterwarnings("ignore")
import pandas as pd

DIR = os.path.dirname(os.path.abspath(__file__))

MATCHED_FILE = os.path.join(DIR, "Raw_Material_Usage_Output.xlsx")
PLU_FILE     = os.path.join(DIR, "PLU_Mapping_Complete.xlsx")
RB_FILE      = os.path.join(DIR, "Recipe builder.xlsx")
OUT_FILE     = "/tmp/Ingredient_Requirements_Report.xlsx"
FINAL_FILE   = os.path.join(DIR, "Ingredient_Requirements_Report.xlsx")

for f in [MATCHED_FILE, PLU_FILE, RB_FILE]:
    if not os.path.exists(f):
        sys.exit(f"ERROR: file not found: {f}")

print("[1/5] Loading matched sales ...")
ms = pd.read_excel(MATCHED_FILE, sheet_name="Matched Sales", dtype=str, engine="calamine")
ms.columns = ms.columns.str.strip()
ms["Quantity"] = pd.to_numeric(ms["Quantity"], errors="coerce").fillna(0)
ms["Item_id"]    = ms["Item_id"].fillna("").str.strip()
ms["Option_id_1"]= ms["Option_id_1"].fillna("").str.strip()
ms["Option_id_2"]= ms["Option_id_2"].fillna("").str.strip()
ms["Store"]      = ms["Store"].fillna("Unknown").str.strip()
ms["Brand"]      = ms["Brand"].fillna("").str.strip()
ms["Recipe_Item"]= ms["Recipe_Item"].fillna("").str.strip()
print(f"  Matched Sales rows (total): {len(ms):,}")

# Exclude soft drinks / beverages — no ingredient recipe needed
_DRINK_KW = ["coke","pepsi","sprite","fanta","7 up","7up","tango orange",
             "water can","sparkling can","still water"]
_is_drink = lambda n: any(kw in n.lower() for kw in _DRINK_KW)
_drink_mask = ms["Recipe_Item"].apply(_is_drink)
print(f"  Excluded drinks:            {_drink_mask.sum():,} rows")
ms = ms[~_drink_mask].copy()
print(f"  Matched Sales rows (used):  {len(ms):,}")

# Aggregate to Brand + Store + Recipe_Item + Item_id + Option_id_1
sales = (ms.groupby(["Brand","Store","Recipe_Item","Item_id","Option_id_1"], dropna=False)
           ["Quantity"].sum().reset_index())
print(f"  Unique Brand+Store+Recipe_Item combos: {len(sales):,}")

print("\n[2/5] Loading Recipe Builder ...")
rb_xl = pd.ExcelFile(RB_FILE, engine="calamine")

# -- Recipe Builder PLU Mapping sheet: Item_id / Option_id -> Menu Item Name --
rb_plu = rb_xl.parse("PLU Mapping", dtype=str)
rb_plu.columns = rb_plu.columns.str.strip()
rb_plu = rb_plu.dropna(subset=["Menu Item Name"])
rb_plu["Item_id"]    = rb_plu["Item_id"].fillna("").str.strip()
rb_plu["Option_id"]  = rb_plu["Option_id"].fillna("").str.strip()
rb_plu["Menu Item Name"] = rb_plu["Menu Item Name"].str.strip()

# Lookup dicts
item_id_to_name   = dict(zip(rb_plu["Item_id"],   rb_plu["Menu Item Name"]))
option_id_to_name = dict(zip(rb_plu["Option_id"], rb_plu["Menu Item Name"]))

# -- Recipe Builder ingredient sheets --
BRAND_SHEETS = {
    "Hot Chick":  "Hot Chick",
    "WTF":        "WTF",
    "Korea Town": "Koreatown",
    "Wing Fest":  "Wing Fest",
    "Kuro Smash": "Kurosmash",
}
rb_frames = []
for brand, sheet in BRAND_SHEETS.items():
    df = rb_xl.parse(sheet, dtype=str)
    df.columns = df.columns.str.strip()
    # Normalise Item_ID -> Item_id (Wing Fest uses uppercase)
    if "Item_ID" in df.columns:
        df = df.rename(columns={"Item_ID": "Item_id"})
    df["_brand"] = brand
    rb_frames.append(df)

rb = pd.concat(rb_frames, ignore_index=True)
rb.columns = rb.columns.str.strip()
rb["Menu Item Name"] = rb["Menu Item Name"].fillna("").str.strip()
rb["Ingredient"]     = rb["Ingredient"].fillna("").str.strip()
rb["Qty_new"]  = pd.to_numeric(rb["Qty_new"],  errors="coerce").fillna(0)
rb["Cost"]     = pd.to_numeric(rb["Cost"],     errors="coerce").fillna(0)
rb["UOM_new"]  = rb["UOM_new"].fillna("").str.strip()
rb["SKU Code"] = rb["SKU Code"].fillna("").str.strip()
rb["Supplier"] = rb["Supplier"].fillna("").str.strip()
rb["Storage Type"] = rb["Storage Type"].fillna("").str.strip()

# Drop blank ingredient rows
rb = rb[rb["Ingredient"] != ""].copy()

print(f"  Recipe Builder ingredient rows (incl. packaging): {len(rb):,}")
print(f"  Unique menu items: {rb['Menu Item Name'].nunique()}")

# -- Sauce resolution: map "Choice of Sauce" placeholder -> actual sauce ingredient --
_SAUCE_MENU_ITEMS = ["Honey Buffalo Sauce", "Korean Sauce", "Ranch Sauce",
                     "BBQ Sauce", "Chilli Mayo Sauce"]
_sauce_rb_raw = rb[rb["Menu Item Name"].isin(_SAUCE_MENU_ITEMS)].copy()

_SAUCE_KW = [
    (["buffalo", "honey buffalo", "extra hot honey", "extra hot"],  "Honey Buffalo Sauce"),
    (["bbq", "smokey", "smoky", "smoked", "burning"],               "BBQ Sauce"),
    (["korean", "seoul", "killer hot korean"],                       "Korean Sauce"),
    (["ranch", "creamy ranch", "original house ranch"],              "Ranch Sauce"),
    (["sriracha", "chilli mayo", "mayo", "scorching"],               "Chilli Mayo Sauce"),
]

def _resolve_sauce_name(sauce_str):
    if not sauce_str or str(sauce_str).lower() in ("nan", "none", ""):
        return None
    s = sauce_str.lower()
    for kws, menu_name in _SAUCE_KW:
        if any(kw in s for kw in kws):
            return menu_name
    return None

# Build lookup: sauce menu item -> ingredient row dict
_sauce_lookup = {}
for smi in _SAUCE_MENU_ITEMS:
    rows = _sauce_rb_raw[_sauce_rb_raw["Menu Item Name"] == smi]
    # Skip packaging rows
    food = rows[~rows["Supplier"].str.lower().str.contains("opal", na=False)]
    if not food.empty:
        r = food.iloc[0]
        _sauce_lookup[smi] = {
            "Ingredient":    r["Ingredient"],
            "SKU Code":      r["SKU Code"],
            "UOM_new":       r["UOM_new"],
            "Supplier":      r["Supplier"],
            "Storage Type":  r["Storage Type"],
            "Cost":          r["Cost"],
        }

print(f"  Sauce lookup entries: {_sauce_lookup}")

print("\n[3/5] Resolving Recipe_Item -> Menu Item Name ...")

import re, difflib

def _normalize(s):
    """Collapse whitespace and lowercase — catches double-space, hyphen-spacing variants."""
    return re.sub(r'\s+', ' ', str(s)).strip().lower()

def _extract_size(s):
    """Return integer size from '(5)', '- 5', '-5' etc., or None."""
    m = re.search(r'\((\d+)\)', s)
    if m: return int(m.group(1))
    m = re.search(r'[-\s](\d+)\s*$', s)
    if m: return int(m.group(1))
    return None

# Build lookup tables for fuzzy/normalized stage
_all_rb_names = [n for n in rb["Menu Item Name"].unique() if n]
_norm_to_rb   = {_normalize(n): n for n in _all_rb_names}   # normalized -> original
_norm_rb_list = list(_norm_to_rb.keys())                     # for difflib

def _fuzzy_resolve(recipe_item):
    """Stage 4: normalized exact, then fuzzy with size preference."""
    norm = _normalize(recipe_item)
    # 4a — exact after normalization (handles double spaces, case)
    if norm in _norm_to_rb:
        return _norm_to_rb[norm], "normalized_exact", None
    # 4b — fuzzy match (cutoff=0.78 keeps false positives low)
    ri_size = _extract_size(recipe_item)
    matches = difflib.get_close_matches(norm, _norm_rb_list, n=5, cutoff=0.78)
    if matches:
        # Prefer a match with same size
        if ri_size is not None:
            for m in matches:
                orig = _norm_to_rb[m]
                if _extract_size(orig) == ri_size:
                    return orig, "fuzzy_same_size", None
        # Fall back to best fuzzy match
        return _norm_to_rb[matches[0]], "fuzzy", None
    # 4c — strip trailing sauce/dip suffix (e.g. "Wings - 5 - Smokey BBQ" -> "Wings - 5")
    parts = recipe_item.rsplit(' - ', 1)
    if len(parts) == 2 and parts[1].strip() and not parts[1].strip().isdigit():
        base  = parts[0].strip()
        sauce = parts[1].strip()
        base_norm = _normalize(base)
        if base_norm in _norm_to_rb:
            return _norm_to_rb[base_norm], "sauce_stripped", sauce
        base_size = _extract_size(base)
        base_matches = difflib.get_close_matches(base_norm, _norm_rb_list, n=3, cutoff=0.78)
        if base_matches:
            if base_size is not None:
                for m in base_matches:
                    if _extract_size(_norm_to_rb[m]) == base_size:
                        return _norm_to_rb[m], "sauce_stripped_fuzzy", sauce
            return _norm_to_rb[base_matches[0]], "sauce_stripped_fuzzy", sauce
    return None, "unmatched", None

def resolve_menu_name(row):
    """Four-stage lookup: Item_id -> Option_id_1 -> direct name -> fuzzy name."""
    if row["Item_id"]:
        n = item_id_to_name.get(row["Item_id"])
        if n:
            return n, "item_id", None
    if row["Option_id_1"]:
        n = option_id_to_name.get(row["Option_id_1"])
        if n:
            return n, "option_id", None
    ri = row["Recipe_Item"]
    all_names = set(rb["Menu Item Name"])
    if ri in all_names:
        return ri, "direct_name", None
    return _fuzzy_resolve(ri)

results = sales.copy()
results[["Menu_Item_Name","Match_Method","Sauce_Name"]] = results.apply(
    lambda r: pd.Series(resolve_menu_name(r)), axis=1
)

matched_sales  = results[results["Menu_Item_Name"].notna()].copy()
unmatched_sales = results[results["Menu_Item_Name"].isna()].copy()

print(f"  Matched:   {len(matched_sales):,} rows ({matched_sales['Quantity'].sum():,.0f} portions)")
print(f"  Unmatched: {len(unmatched_sales):,} rows ({unmatched_sales['Quantity'].sum():,.0f} portions)")
print(f"  Match methods: {matched_sales['Match_Method'].value_counts().to_dict()}")

print("\n[4/5] Computing ingredient quantities ...")

# Join matched sales with Recipe Builder ingredient rows
detail = matched_sales.merge(
    rb[["Menu Item Name","Ingredient","Qty_new","UOM_new","SKU Code","Supplier","Storage Type","Cost"]],
    left_on="Menu_Item_Name", right_on="Menu Item Name", how="left"
)

# Items with no ingredient rows in Recipe Builder
no_recipe = detail[detail["Ingredient"].isna() | (detail["Ingredient"] == "")]
has_recipe = detail[detail["Ingredient"].notna() & (detail["Ingredient"] != "")]

print(f"  Rows with ingredients: {len(has_recipe):,}")
print(f"  Rows with no ingredient data: {len(no_recipe['Recipe_Item'].unique()):,} unique recipes")

# Total raw material = sold qty * ingredient qty per serving
has_recipe = has_recipe.copy()

# Resolve "Choice of Sauce" placeholder rows to actual sauce ingredients
_choice_mask = has_recipe["Ingredient"].str.strip().str.lower() == "choice of sauce"
print(f"  Resolving Choice of Sauce rows: {_choice_mask.sum():,}")
if _choice_mask.any():
    def _fix_sauce_row(row):
        sauce_menu = _resolve_sauce_name(row.get("Sauce_Name"))
        if sauce_menu and sauce_menu in _sauce_lookup:
            sl = _sauce_lookup[sauce_menu]
            return pd.Series(sl)
        return pd.Series({k: row[k] for k in
                          ["Ingredient","SKU Code","UOM_new","Supplier","Storage Type","Cost"]})
    resolved = has_recipe[_choice_mask].apply(_fix_sauce_row, axis=1)
    has_recipe.loc[_choice_mask,
        ["Ingredient","SKU Code","UOM_new","Supplier","Storage Type","Cost"]] = resolved.values

has_recipe["Total_Raw_Qty"] = has_recipe["Quantity"] * has_recipe["Qty_new"]
has_recipe["Total_Cost"]    = has_recipe["Quantity"] * has_recipe["Cost"]

# -- Site Raw Material: Brand + Store + SKU --
site_raw = (has_recipe
    .groupby(["Brand","Store","SKU Code","Ingredient","Supplier","Storage Type","UOM_new"], dropna=False)
    .agg(Total_Raw_Qty=("Total_Raw_Qty","sum"), Total_Cost=("Total_Cost","sum"))
    .reset_index()
    .rename(columns={"SKU Code":"SKU","UOM_new":"UOM"})
    .sort_values(["Brand","Store","SKU"]))

# -- Raw Material Summary: Brand + SKU --
raw_summary = (has_recipe
    .groupby(["Brand","SKU Code","Ingredient","Supplier","Storage Type","UOM_new"], dropna=False)
    .agg(Total_Raw_Qty=("Total_Raw_Qty","sum"), Total_Cost=("Total_Cost","sum"),
         Sold_Qty=("Quantity","sum"))
    .reset_index()
    .rename(columns={"SKU Code":"SKU","UOM_new":"UOM"})
    .sort_values(["Brand","SKU"]))

# -- SKU summary (all brands combined) --
ingredient_summary = (has_recipe
    .groupby(["SKU Code","Ingredient","Supplier","Storage Type","UOM_new"], dropna=False)
    .agg(Total_Raw_Qty=("Total_Raw_Qty","sum"), Total_Cost=("Total_Cost","sum"))
    .reset_index()
    .rename(columns={"SKU Code":"SKU","UOM_new":"UOM"})
    .sort_values("SKU"))

# -- Unmatched report --
unmatched_report = pd.concat([
    unmatched_sales[["Brand","Store","Recipe_Item","Item_id","Option_id_1","Quantity","Match_Method"]],
    no_recipe[["Brand","Store","Recipe_Item","Item_id","Option_id_1","Quantity","Menu_Item_Name"]].assign(
        Match_Method="no_recipe_in_builder"
    ).rename(columns={"Menu_Item_Name":"Resolved_Name"})
], ignore_index=True).fillna("")

print(f"\n  Site Raw Material rows: {len(site_raw):,}")
print(f"  Raw Summary rows:       {len(raw_summary):,}")
print(f"  Unique ingredients:     {ingredient_summary['Ingredient'].nunique()}")
print(f"  Unmatched/no-recipe:    {len(unmatched_report):,}")

print("\n[5/5] Writing output ...")

import xlsxwriter, shutil

def _clean(v):
    if v is None: return ""
    if isinstance(v, float) and pd.isna(v): return ""
    return v

xwb = xlsxwriter.Workbook(OUT_FILE, {"strings_to_numbers": False, "nan_inf_to_errors": True})
fmt_cache = {}

def xfmt(bg=None, bold=False, size=10, color="000000", align="left", wrap=False, border=True):
    key = (bg, bold, size, color, align, wrap, border)
    if key not in fmt_cache:
        f = xwb.add_format({"font_name":"Calibri","font_size":size,"font_color":color,
                             "bold":bold,"align":align,"valign":"vcenter","text_wrap":wrap})
        if bg: f.set_bg_color(bg)
        if border: f.set_border(1); f.set_border_color("#D0D0D0")
        fmt_cache[key] = f
    return fmt_cache[key]

HDR   = xfmt(bg="#1F3864", bold=True, color="FFFFFF", align="center", wrap=True)
PLAIN = xfmt()

BRAND_COLORS = {
    "Hot Chick":  "#FDE9D9",
    "WTF":        "#F4ECF7",
    "Korea Town": "#FDEDEC",
    "Wing Fest":  "#E9F7EF",
    "Kuro Smash": "#EAECEE",
}

def write_sheet(ws, df, col_widths, default_fmt=None):
    cols = list(df.columns)
    for ci, col in enumerate(cols):
        ws.write(0, ci, col, HDR)
    ws.set_row(0, 26)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols)-1)
    for ci, w in enumerate(col_widths):
        ws.set_column(ci, ci, w)
    rfmt = default_fmt or PLAIN
    for ci, col in enumerate(cols):
        ws.write_column(1, ci, [_clean(v) for v in df[col]], rfmt)

# -- Dashboard --
ws_dash = xwb.add_worksheet("Dashboard")
ws_dash.set_column(0, 0, 40); ws_dash.set_column(1, 1, 20)
b14 = xfmt(bold=True, size=14, border=False)
b12 = xfmt(bold=True, size=12, border=False)
n11 = xfmt(size=11, border=False)
b11 = xfmt(bold=True, size=11, border=False)

ws_dash.write(0, 0, "Ingredient Requirements Report", b14)
ws_dash.write(1, 0, "Source: Raw_Material_Usage_Output.xlsx + Recipe builder.xlsx", n11)
r = 3
ws_dash.write(r, 0, "Summary", b12); r += 1
for lbl, val in [
    ("Total portions matched to recipes", int(has_recipe["Quantity"].sum())),
    ("Total portions unmatched/no-recipe", int(unmatched_report["Quantity"].fillna(0).astype(float).sum())),
    ("Unique ingredients identified",      ingredient_summary["Ingredient"].nunique()),
    ("Unique sites (stores)",              site_raw["Store"].nunique()),
    ("Unique recipe items resolved",       matched_sales["Menu_Item_Name"].nunique()),
]:
    ws_dash.write(r, 0, lbl, n11); ws_dash.write(r, 1, val, b11); r += 1

r += 1
ws_dash.write(r, 0, "By Brand", b12); r += 1
brand_totals = (has_recipe.groupby("Brand")
    .agg(Portions=("Quantity","sum"), Total_Cost=("Total_Cost","sum")).reset_index())
for _, brow in brand_totals.iterrows():
    bg = BRAND_COLORS.get(brow["Brand"])
    f  = xfmt(bg=bg, border=False)
    fb = xfmt(bg=bg, bold=True, border=False)
    ws_dash.write(r, 0, brow["Brand"], f)
    ws_dash.write(r, 1, f"{int(brow['Portions']):,} portions | \xa3{brow['Total_Cost']:,.2f} cost", fb)
    r += 1

# -- Site Raw Material (Brand + Store + SKU) --
ws_site = xwb.add_worksheet("Site Raw Material")
write_sheet(ws_site, site_raw,
    [16, 40, 14, 36, 18, 14, 10, 16, 14])

# -- Raw Material Summary (Brand + SKU) --
ws_summ = xwb.add_worksheet("Raw Material Summary")
write_sheet(ws_summ, raw_summary,
    [16, 14, 36, 18, 14, 10, 16, 14, 12])

# -- SKU Summary (all brands) --
ws_ing = xwb.add_worksheet("SKU Summary")
write_sheet(ws_ing, ingredient_summary, [14, 36, 18, 14, 10, 16, 14])

# -- Unmatched --
ws_um = xwb.add_worksheet("Unmatched")
RED = xfmt(bg="#FADBD8")
write_sheet(ws_um, unmatched_report, [16,36,40,16,16,12,28], default_fmt=RED)

xwb.close()
shutil.copy2(OUT_FILE, FINAL_FILE)
print(f"\nOK Saved -> {FINAL_FILE}")
print(f"\nResults:")
print(f"  Site Raw Material rows:      {len(site_raw):,}")
print(f"  Unique SKUs:                 {ingredient_summary['SKU'].nunique()}")
print(f"  Unique sites:                {site_raw['Store'].nunique()}")
print(f"  Total estimated cost:        GBP {has_recipe['Total_Cost'].sum():,.2f}")
