"""
Recipe Usage Engine  — vectorised pandas implementation
=========================================================
Maps item-wise and option-wise sales reports against PLU_Mapping_Complete.xlsx
using rule-based priority matching.

Usage:
    python recipe_usage_engine.py

Inputs  (same folder):
    PLU_Mapping_Complete.xlsx
    Items-wise-order-transactions-*.xlsx
    Options-wise-order-transactions-*.xlsx

Output:
    Raw_Material_Usage_Output.xlsx
"""

import os, sys, glob, warnings
warnings.filterwarnings("ignore")
import pandas as pd
from collections import Counter

# -- Config --------------------------------------------------------------------
DIR = os.path.dirname(os.path.abspath(__file__))

def find(pattern):
    m = glob.glob(os.path.join(DIR, pattern))
    if not m:
        sys.exit(f"ERROR: no file matching '{pattern}' found in {DIR}")
    return max(m, key=os.path.getmtime)

PLU_FILE     = find("PLU_Mapping_Complete.xlsx")
ITEMS_FILE   = find("Items-wise-order-transactions-*.xlsx")
OPTIONS_FILE = find("Options-wise-order-transactions-*.xlsx")
_FINAL_FILE  = os.path.join(DIR, "Raw_Material_Usage_Output.xlsx")
OUT_FILE     = "/tmp/Raw_Material_Usage_Output.xlsx"

IN_SCOPE = {"Hot Chick", "WTF", "Korea Town", "Wing Fest", "Kuro Smash", "Twisted London"}

print(f"PLU:     {os.path.basename(PLU_FILE)}")
print(f"Items:   {os.path.basename(ITEMS_FILE)}")
print(f"Options: {os.path.basename(OPTIONS_FILE)}")

# -----------------------------------------------------------------------------
# 1.  LOAD & PREPARE DATA
# -----------------------------------------------------------------------------
print("\n[1/5] Loading data ...")

# PLU Mapping
if os.path.exists("/tmp/plu.pkl"):
    plu = pd.read_pickle("/tmp/plu.pkl")
    plu = plu.fillna("").apply(lambda c: c.str.strip() if c.dtype == object else c)
else:
    plu = (pd.read_excel(PLU_FILE, sheet_name="Master PLU Mapping", dtype=str, engine="calamine")
             .fillna("").apply(lambda c: c.str.strip() if c.dtype == object else c))

# Items report
if os.path.exists("/tmp/items.pkl"):
    items = pd.read_pickle("/tmp/items.pkl")
    items.columns = items.columns.str.strip()
else:
    items = pd.read_excel(ITEMS_FILE, dtype=str, engine="calamine")
    items.columns = items.columns.str.strip()
items = items[items["Brand"].isin(IN_SCOPE)].copy()
items["Quantity"] = pd.to_numeric(items["Quantity"], errors="coerce").fillna(0)
for c in ["Item ref ID", "unique id", "Brand", "Store", "Order ID"]:
    if c in items.columns:
        items[c] = items[c].astype(str).str.strip()

# Options report
if os.path.exists("/tmp/opts.pkl"):
    opts = pd.read_pickle("/tmp/opts.pkl")
    opts.columns = opts.columns.str.strip()
else:
    opts = pd.read_excel(OPTIONS_FILE, dtype=str, engine="calamine")
    opts.columns = opts.columns.str.strip()
opts = opts[opts["Brand"].isin(IN_SCOPE)].copy()
opts["Option Quantity"] = pd.to_numeric(opts["Option Quantity"], errors="coerce").fillna(0)
for c in ["Option Ref ID", "unique id", "Brand", "Store Name", "Order ID"]:
    if c in opts.columns:
        opts[c] = opts[c].astype(str).str.strip()
opts["parent_uid"] = opts["unique id"].str.rsplit("-", n=1).str[0]
opts["_opt_row_id"] = range(len(opts))  # stable row id for selector tracking

print(f"  Items (in-scope):   {len(items):,}")
print(f"  Options (in-scope): {len(opts):,}")

# -----------------------------------------------------------------------------
# 2.  BUILD MAPPING LOOKUP TABLES (as DataFrames)
# -----------------------------------------------------------------------------
print("\n[2/5] Building lookup tables ...")

# Brand comes from sales data — exclude it from lookup tables to avoid _x/_y suffix conflicts
LKP_META = ["Category","Recipe_Item"]

def plu_subset(rule, rename=None):
    df = plu[plu["Rule_Type"] == rule].copy()
    if rename:
        df = df.rename(columns=rename)
    return df

item_only_lkp = plu_subset("ITEM_ONLY",  {"Item_id":"Item ref ID"})[["Item ref ID"]+LKP_META]
meal_deal_lkp = plu_subset("MEAL_DEAL",  {"Item_id":"Item ref ID"})[["Item ref ID"]+LKP_META]
combo_lkp     = plu_subset("ITEM_OPTION_COMBO",
                            {"Item_id":"Item ref ID","Option_id_1":"opt_ref_1"}
                            )[["Item ref ID","opt_ref_1"]+LKP_META]
multi_lkp_raw = plu_subset("ITEM_MULTI_OPTION_COMBO",
                            {"Item_id":"Item ref ID","Option_id_1":"opt1","Option_id_2":"opt2"}
                            )[["Item ref ID","opt1","opt2"]+LKP_META]
# Store both orderings for multi so merge is order-independent
multi_lkp = pd.concat([
    multi_lkp_raw.rename(columns={"opt1":"opt_ref_1","opt2":"opt_ref_2"}),
    multi_lkp_raw.rename(columns={"opt2":"opt_ref_1","opt1":"opt_ref_2"}),
], ignore_index=True).drop_duplicates(subset=["Item ref ID","opt_ref_1","opt_ref_2"])

component_lkp = (plu_subset("OPTION_COMPONENT", {"Option_id_1":"Option Ref ID"})
                 [["Option Ref ID","Category","Recipe_Item"]])
addon_lkp     = (plu_subset("STANDALONE_ADDON", {"Option_id_1":"Option Ref ID"})
                 [["Option Ref ID","Category","Recipe_Item"]])

print(f"  ITEM_ONLY {len(item_only_lkp)}  MEAL_DEAL {len(meal_deal_lkp)}"
      f"  COMBO {len(combo_lkp)}  MULTI {len(multi_lkp)//2}"
      f"  COMPONENT {len(component_lkp)}  ADDON {len(addon_lkp)}")

# Slim options table for joining with items
opts_slim = opts[["parent_uid","unique id","Option Ref ID","_opt_row_id"]].rename(
    columns={"unique id":"opt_uid"})

# -----------------------------------------------------------------------------
# 3.  PRIORITY MATCHING (vectorised)
# -----------------------------------------------------------------------------
print("\n[3/5] Matching item rows ...")

# Tracking sets
matched_item_uids  = set()   # items already claimed
selector_opt_rows  = set()   # option _opt_row_ids used as selectors
result_parts       = []      # list of DataFrames to concat at end

# -- Helper: tag result columns ------------------------------------------------
BASE_COLS = ["Brand","Store","Order ID","unique id","Category","Recipe_Item",
             "Rule_Type","Item_id","Option_id_1","Option_id_2","Quantity","Qty_Source"]

STORE_COL = "Store" if "Store" in items.columns else (
            "Store Name" if "Store Name" in items.columns else "Order ID")

def emit(df, rule, item_id_col, opt1_col, opt2_col, qty_col, qty_src):
    """Standardise a matched result chunk and add to result_parts."""
    out = pd.DataFrame()
    out["Brand"]       = df["Brand"] if "Brand" in df.columns else ""
    out["Store"]       = df[STORE_COL] if STORE_COL in df.columns else ""
    out["Order_ID"]    = df["Order ID"] if "Order ID" in df.columns else ""
    out["Unique_ID"]   = df[item_id_col]
    out["Category"]    = df["Category"]
    out["Recipe_Item"] = df["Recipe_Item"]
    out["Rule_Type"]   = rule
    out["Item_id"]     = df["Item ref ID"] if "Item ref ID" in df.columns else ""
    out["Option_id_1"] = df[opt1_col] if opt1_col in df.columns else ""
    out["Option_id_2"] = df[opt2_col] if opt2_col in df.columns else ""
    out["Quantity"]    = df[qty_col]
    out["Qty_Source"]  = qty_src
    result_parts.append(out)

# ============================================================================
# A. ITEM_MULTI_OPTION_COMBO
# ============================================================================
multi_item_ids = set(multi_lkp["Item ref ID"])
items_multi_cand = items[items["Item ref ID"].isin(multi_item_ids) &
                         ~items["unique id"].isin(matched_item_uids)]

# Join items with option1
step1 = items_multi_cand.merge(
    opts_slim.rename(columns={"Option Ref ID":"opt_ref_1",
                               "opt_uid":"opt_uid_1",
                               "_opt_row_id":"opt_row_id_1"}),
    left_on="unique id", right_on="parent_uid", how="inner"
)
# Join with option2 (different option)
step2 = step1.merge(
    opts_slim.rename(columns={"Option Ref ID":"opt_ref_2",
                               "opt_uid":"opt_uid_2",
                               "_opt_row_id":"opt_row_id_2"}),
    on="parent_uid", how="inner"
)
step2 = step2[step2["opt_ref_1"] != step2["opt_ref_2"]]  # exclude self-pairs

# Merge with multi mapping
multi_hit = step2.merge(multi_lkp, on=["Item ref ID","opt_ref_1","opt_ref_2"], how="inner")

# Keep one hit per item unique_id (first match wins)
multi_hit = multi_hit.drop_duplicates(subset=["unique id"])

if len(multi_hit):
    matched_item_uids.update(multi_hit["unique id"])
    selector_opt_rows.update(multi_hit["opt_row_id_1"])
    selector_opt_rows.update(multi_hit["opt_row_id_2"])
    emit(multi_hit, "ITEM_MULTI_OPTION_COMBO",
         "unique id", "opt_ref_1", "opt_ref_2", "Quantity", "item_qty")

print(f"  ITEM_MULTI_OPTION_COMBO: {len(multi_hit):,}")

# ============================================================================
# B. ITEM_OPTION_COMBO
# ============================================================================
combo_item_ids = set(combo_lkp["Item ref ID"])
items_combo_cand = items[items["Item ref ID"].isin(combo_item_ids) &
                          ~items["unique id"].isin(matched_item_uids)]

combo_step = items_combo_cand.merge(
    opts_slim.rename(columns={"Option Ref ID":"opt_ref_1",
                               "opt_uid":"opt_uid_1",
                               "_opt_row_id":"opt_row_id_1"}),
    left_on="unique id", right_on="parent_uid", how="inner"
)
combo_hit = combo_step.merge(combo_lkp, on=["Item ref ID","opt_ref_1"], how="inner")
combo_hit = combo_hit.drop_duplicates(subset=["unique id"])

if len(combo_hit):
    matched_item_uids.update(combo_hit["unique id"])
    selector_opt_rows.update(combo_hit["opt_row_id_1"])
    emit(combo_hit, "ITEM_OPTION_COMBO",
         "unique id", "opt_ref_1", "", "Quantity", "item_qty")

print(f"  ITEM_OPTION_COMBO:       {len(combo_hit):,}")

# ============================================================================
# C. ITEM_ONLY
# ============================================================================
items_io_cand = items[items["Item ref ID"].isin(set(item_only_lkp["Item ref ID"])) &
                      ~items["unique id"].isin(matched_item_uids)]
io_hit = items_io_cand.merge(item_only_lkp, on="Item ref ID", how="inner")
io_hit = io_hit.drop_duplicates(subset=["unique id"])

if len(io_hit):
    matched_item_uids.update(io_hit["unique id"])
    emit(io_hit, "ITEM_ONLY", "unique id", "", "", "Quantity", "item_qty")

print(f"  ITEM_ONLY:               {len(io_hit):,}")

# ============================================================================
# D. MEAL_DEAL
# ============================================================================
items_md_cand = items[items["Item ref ID"].isin(set(meal_deal_lkp["Item ref ID"])) &
                      ~items["unique id"].isin(matched_item_uids)]
md_hit = items_md_cand.merge(meal_deal_lkp, on="Item ref ID", how="inner")
md_hit = md_hit.drop_duplicates(subset=["unique id"])

meal_deal_uids = set()
if len(md_hit):
    matched_item_uids.update(md_hit["unique id"])
    meal_deal_uids = set(md_hit["unique id"])
    emit(md_hit, "MEAL_DEAL", "unique id", "", "", "Quantity", "item_qty")

print(f"  MEAL_DEAL:               {len(md_hit):,}")

# -- Unmatched items -----------------------------------------------------------
unmatched_items_df = items[~items["unique id"].isin(matched_item_uids)].copy()
print(f"  Unmatched items:         {len(unmatched_items_df):,}")

# -----------------------------------------------------------------------------
# 4.  OPTION ROWS
# -----------------------------------------------------------------------------
print("\n[4/5] Matching option rows ...")

# Remaining options = not used as selectors
opts_remaining = opts[~opts["_opt_row_id"].isin(selector_opt_rows)].copy()

# -- OPTION_COMPONENT: parent must be a MEAL_DEAL uid -------------------------
comp_cand = opts_remaining[opts_remaining["parent_uid"].isin(meal_deal_uids)]
comp_hit  = comp_cand.merge(component_lkp, on="Option Ref ID", how="inner")
comp_hit  = comp_hit.drop_duplicates(subset=["unique id"])

used_opt_uids = set()
if len(comp_hit):
    used_opt_uids.update(comp_hit["_opt_row_id"])
    # emit with option qty
    out = pd.DataFrame()
    out["Brand"]       = comp_hit["Brand"]
    out["Store"]       = comp_hit["Store Name"]
    out["Order_ID"]    = comp_hit["Order ID"]
    out["Unique_ID"]   = comp_hit["unique id"]
    out["Category"]    = comp_hit["Category"]
    out["Recipe_Item"] = comp_hit["Recipe_Item"]
    out["Rule_Type"]   = "OPTION_COMPONENT"
    out["Item_id"]     = ""
    out["Option_id_1"] = comp_hit["Option Ref ID"]
    out["Option_id_2"] = ""
    out["Quantity"]    = comp_hit["Option Quantity"]
    out["Qty_Source"]  = "option_qty"
    result_parts.append(out)

print(f"  OPTION_COMPONENT:        {len(comp_hit):,}")

# -- STANDALONE_ADDON: any parent ---------------------------------------------
addon_cand = opts_remaining[~opts_remaining["_opt_row_id"].isin(used_opt_uids)]
addon_hit  = addon_cand.merge(addon_lkp, on="Option Ref ID", how="inner")
addon_hit  = addon_hit.drop_duplicates(subset=["unique id"])

if len(addon_hit):
    used_opt_uids.update(addon_hit["_opt_row_id"])
    out = pd.DataFrame()
    out["Brand"]       = addon_hit["Brand"]
    out["Store"]       = addon_hit["Store Name"]
    out["Order_ID"]    = addon_hit["Order ID"]
    out["Unique_ID"]   = addon_hit["unique id"]
    out["Category"]    = addon_hit["Category"]
    out["Recipe_Item"] = addon_hit["Recipe_Item"]
    out["Rule_Type"]   = "STANDALONE_ADDON"
    out["Item_id"]     = ""
    out["Option_id_1"] = addon_hit["Option Ref ID"]
    out["Option_id_2"] = ""
    out["Quantity"]    = addon_hit["Option Quantity"]
    out["Qty_Source"]  = "option_qty"
    result_parts.append(out)

print(f"  STANDALONE_ADDON:        {len(addon_hit):,}")

# ============================================================================
# ORPHANED OPTIONS PASS
# Some orders have options in the Options report but NO corresponding item
# row in the Items report.  Handle these separately.
# ============================================================================
all_item_uids = set(items["unique id"])   # every uid that EXISTS in items report
still_remaining = opts_remaining[~opts_remaining["_opt_row_id"].isin(used_opt_uids)].copy()

# Options whose parent_uid has NO item row at all
orphaned = still_remaining[~still_remaining["parent_uid"].isin(all_item_uids)].copy()
orphaned_used = set()

# -- Orphaned OPTION_COMPONENT: match directly, no parent MEAL_DEAL required -
orp_comp = orphaned.merge(component_lkp, on="Option Ref ID", how="inner")
orp_comp = orp_comp.drop_duplicates(subset=["unique id"])
if len(orp_comp):
    orphaned_used.update(orp_comp["_opt_row_id"])
    out = pd.DataFrame()
    out["Brand"]       = orp_comp["Brand"]
    out["Store"]       = orp_comp["Store Name"]
    out["Order_ID"]    = orp_comp["Order ID"]
    out["Unique_ID"]   = orp_comp["unique id"]
    out["Category"]    = orp_comp["Category"]
    out["Recipe_Item"] = orp_comp["Recipe_Item"]
    out["Rule_Type"]   = "OPTION_COMPONENT"
    out["Item_id"]     = ""
    out["Option_id_1"] = orp_comp["Option Ref ID"]
    out["Option_id_2"] = ""
    out["Quantity"]    = orp_comp["Option Quantity"]
    out["Qty_Source"]  = "option_qty"
    result_parts.append(out)
print(f"  Orphaned OPTION_COMPONENT: {len(orp_comp):,}")

# -- Orphaned ITEM_MULTI selectors: pair siblings, look up recipe -------------
# Build a reverse map: (opt1, opt2) → recipe row  (from multi_lkp)
# For orphaned options, group by parent_uid and try all pairs
orp_remaining = orphaned[~orphaned["_opt_row_id"].isin(orphaned_used)].copy()

# Self-join on parent_uid to get pairs
orp_pairs = orp_remaining.merge(
    orp_remaining[["parent_uid","Option Ref ID","_opt_row_id"]].rename(
        columns={"Option Ref ID":"opt_ref_2","_opt_row_id":"opt_row_id_2"}),
    on="parent_uid", how="inner"
)
orp_pairs = orp_pairs[orp_pairs["Option Ref ID"] != orp_pairs["opt_ref_2"]]

# Merge with multi_lkp (which has both orderings)
orp_multi_hit = orp_pairs.merge(
    multi_lkp.rename(columns={"opt_ref_1":"Option Ref ID","opt_ref_2":"opt_ref_2"}),
    on=["Option Ref ID","opt_ref_2"], how="inner"
)
# One result per option row (the first selector in the pair)
orp_multi_hit = orp_multi_hit.drop_duplicates(subset=["unique id"])
# Deduplicate so each parent_uid only emits one matched recipe per unique combination
orp_multi_hit = orp_multi_hit.drop_duplicates(subset=["parent_uid","Recipe_Item"])

if len(orp_multi_hit):
    orphaned_used.update(orp_multi_hit["_opt_row_id"])
    orphaned_used.update(orp_multi_hit["opt_row_id_2"])
    out = pd.DataFrame()
    out["Brand"]       = orp_multi_hit["Brand"]
    out["Store"]       = orp_multi_hit["Store Name"]
    out["Order_ID"]    = orp_multi_hit["Order ID"]
    out["Unique_ID"]   = orp_multi_hit["unique id"]
    out["Category"]    = orp_multi_hit["Category"]
    out["Recipe_Item"] = orp_multi_hit["Recipe_Item"]
    out["Rule_Type"]   = "ITEM_MULTI_OPTION_COMBO"
    out["Item_id"]     = orp_multi_hit["Item ref ID"] if "Item ref ID" in orp_multi_hit.columns else ""
    out["Option_id_1"] = orp_multi_hit["Option Ref ID"]
    out["Option_id_2"] = orp_multi_hit["opt_ref_2"]
    out["Quantity"]    = orp_multi_hit["Option Quantity"]
    out["Qty_Source"]  = "option_qty"
    result_parts.append(out)
print(f"  Orphaned ITEM_MULTI:       {len(orp_multi_hit):,}")

used_opt_uids.update(orphaned_used)

unmatched_opts_df = opts_remaining[
    ~opts_remaining["_opt_row_id"].isin(used_opt_uids)
].copy()
print(f"  Unmatched options:       {len(unmatched_opts_df):,}")

# -- Consolidate matched results -----------------------------------------------
matched_df = pd.concat(result_parts, ignore_index=True) if result_parts else pd.DataFrame(columns=["Brand","Store","Order_ID","Unique_ID","Category","Recipe_Item","Rule_Type","Item_id","Option_id_1","Option_id_2","Quantity","Qty_Source"])
matched_df["Quantity"] = pd.to_numeric(matched_df["Quantity"], errors="coerce").fillna(0)

print(f"\n  TOTAL matched rows: {len(matched_df):,}")

# -- Recipe Summary -------------------------------------------------------------
summary_df = (matched_df.groupby(["Brand","Category","Recipe_Item","Rule_Type"], dropna=False)["Quantity"]
                         .sum().reset_index()
                         .rename(columns={"Quantity":"Total_Qty"})
                         .sort_values(["Brand","Category","Recipe_Item"]))

# -----------------------------------------------------------------------------
# 5.  WRITE EXCEL OUTPUT  (xlsxwriter — fast bulk writes)
# -----------------------------------------------------------------------------
print("\n[5/5] Writing output ...", flush=True)

import xlsxwriter
print("  xlsxwriter imported", flush=True)

RULE_COLORS = {
    "ITEM_ONLY":               "#DDEEFF",
    "ITEM_OPTION_COMBO":       "#D5F5E3",
    "ITEM_MULTI_OPTION_COMBO": "#A9DFBF",
    "MEAL_DEAL":               "#FEF9E7",
    "OPTION_COMPONENT":        "#FAD7A0",
    "STANDALONE_ADDON":        "#F5CBA7",
}
BRAND_COLORS = {
    "Hot Chick":  "#FDE9D9",
    "WTF":        "#F4ECF7",
    "Korea Town": "#FDEDEC",
    "Wing Fest":  "#E9F7EF",
    "Kuro Smash": "#EAECEE",
}

rule_cnt  = matched_df["Rule_Type"].value_counts().to_dict()
brand_cnt = matched_df["Brand"].value_counts().to_dict()

xwb = xlsxwriter.Workbook(OUT_FILE, {"strings_to_numbers": False, "nan_inf_to_errors": True})
fmt_cache = {}

def xfmt(bg=None, bold=False, size=10, color="000000", align="left", wrap=False, border=False):
    key = (bg, bold, size, color, align, wrap, border)
    if key not in fmt_cache:
        f = xwb.add_format({"font_name":"Calibri","font_size":size,"font_color":color,
                             "bold":bold,"align":align,"valign":"vcenter","text_wrap":wrap})
        if bg:   f.set_bg_color(bg)
        if border: f.set_border(1); f.set_border_color("#D0D0D0")
        fmt_cache[key] = f
    return fmt_cache[key]

HDR = xfmt(bg="#1F3864", bold=True, color="FFFFFF", align="center", wrap=True, border=True)
PLAIN = xfmt(border=True)
RED_FMT = xfmt(bg="#FADBD8", border=True)
ORG_FMT = xfmt(bg="#FDEBD0", border=True)

def _clean(v):
    if v is None: return ""
    if isinstance(v, float) and pd.isna(v): return ""
    return v

def write_xsheet(ws, df, col_widths, row_color_fn=None, default_fmt=None):
    """Write a DataFrame to an xlsxwriter worksheet."""
    cols = list(df.columns)
    for ci, col in enumerate(cols):
        ws.write(0, ci, col, HDR)
    ws.set_row(0, 26)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols)-1)
    for ci, w in enumerate(col_widths):
        ws.set_column(ci, ci, w)
    if row_color_fn is None:
        rfmt = default_fmt or PLAIN
        for ci, col in enumerate(cols):
            ws.write_column(1, ci, [_clean(v) for v in df[col]], rfmt)
    else:
        for ri, row in enumerate(df.itertuples(index=False), start=1):
            bg = row_color_fn(row)
            rfmt = xfmt(bg=bg, border=True) if bg else (default_fmt or PLAIN)
            ws.write_row(ri, 0, [_clean(v) for v in row], rfmt)

# -- Shee
# -- Sheet 1: Dashboard -------------------------------------------------------
ws_dash = xwb.add_worksheet("Dashboard")
ws_dash.set_column(0, 0, 36); ws_dash.set_column(1, 1, 16)
b14 = xfmt(bold=True, size=14); b12 = xfmt(bold=True, size=12)
n11 = xfmt(size=11); b11 = xfmt(bold=True, size=11)
ws_dash.write(0, 0, "Recipe Usage Engine - Results", b14)
ws_dash.write(1, 0, f"Items:   {os.path.basename(ITEMS_FILE)}", n11)
ws_dash.write(2, 0, f"Options: {os.path.basename(OPTIONS_FILE)}", n11)
r = 4
ws_dash.write(r, 0, "Overall", b12); r += 1
for lbl, val in [("Total matched rows", len(matched_df)),
                  ("Unmatched item rows", len(unmatched_items_df)),
                  ("Unmatched option rows", len(unmatched_opts_df)),
                  ("Unique recipes matched", len(summary_df))]:
    ws_dash.write(r, 0, lbl, n11); ws_dash.write(r, 1, val, b11); r += 1
r += 1
ws_dash.write(r, 0, "By Rule Type", b12); r += 1
RULE_COLORS = {
    "ITEM_ONLY":               "#DDEEFF",
    "ITEM_OPTION_COMBO":       "#D5F5E3",
    "ITEM_MULTI_OPTION_COMBO": "#A9DFBF",
    "MEAL_DEAL":               "#FEF9E7",
    "OPTION_COMPONENT":        "#FAD7A0",
    "STANDALONE_ADDON":        "#F5CBA7",
}
BRAND_COLORS = {
    "Hot Chick":  "#FDE9D9",
    "WTF":        "#F4ECF7",
    "Korea Town": "#FDEDEC",
    "Wing Fest":  "#E9F7EF",
    "Kuro Smash": "#EAECEE",
}
for rule in ["ITEM_ONLY","ITEM_OPTION_COMBO","ITEM_MULTI_OPTION_COMBO",
             "MEAL_DEAL","OPTION_COMPONENT","STANDALONE_ADDON"]:
    bg = RULE_COLORS.get(rule)
    f = xfmt(bg=bg); fb = xfmt(bg=bg, bold=True)
    ws_dash.write(r, 0, rule, f); ws_dash.write(r, 1, rule_cnt.get(rule,0), fb); r += 1
r += 1
ws_dash.write(r, 0, "By Brand", b12); r += 1
for brand in ["Hot Chick","WTF","Korea Town","Wing Fest","Kuro Smash"]:
    bg = BRAND_COLORS.get(brand)
    f = xfmt(bg=bg); fb = xfmt(bg=bg, bold=True)
    ws_dash.write(r, 0, brand, f); ws_dash.write(r, 1, brand_cnt.get(brand,0), fb); r += 1

# -- Sheet 2: Matched Sales ---------------------------------------------------
ws_matched = xwb.add_worksheet("Matched Sales")
write_xsheet(ws_matched, matched_df, [14,24,14,24,16,42,28,16,16,16,10,12])

# -- Sheet 3: Recipe Summary --------------------------------------------------
ws_summ = xwb.add_worksheet("Recipe Summary")
def summ_color(row):
    return RULE_COLORS.get(getattr(row, "Rule_Type", None))
write_xsheet(ws_summ, summary_df, [14,18,44,28,14], row_color_fn=summ_color)

# -- Sheet 4: Unmatched Items -------------------------------------------------
ws_ui = xwb.add_worksheet("Unmatched Items")
ui_cols_list = ["Brand","Item ref ID","Item Name","Quantity","Store","Order ID","unique id"]
ui_out = unmatched_items_df[[c for c in ui_cols_list if c in unmatched_items_df.columns]].copy()
write_xsheet(ws_ui, ui_out, [14,16,36,10,24,14,24], default_fmt=RED_FMT)

# -- Sheet 5: Unmatched Options -----------------------------------------------
ws_uo = xwb.add_worksheet("Unmatched Options")
uo_cols_list = ["Brand","Option Ref ID","Option Title","Option Quantity","parent_uid","Store Name","Order ID","unique id"]
uo_out = unmatched_opts_df[[c for c in uo_cols_list if c in unmatched_opts_df.columns]].copy()
write_xsheet(ws_uo, uo_out, [14,28,36,10,24,24,14,24], default_fmt=ORG_FMT)

print(f"  Closing workbook -> {OUT_FILE}", flush=True)
xwb.close()
print(f"  Closed. Copying to {_FINAL_FILE}", flush=True)
import shutil as _sh
_sh.copy2(OUT_FILE, _FINAL_FILE)
print(f"\nOK Saved -> {_FINAL_FILE}")
print(f"\nResults:")
print(f"  Matched rows:          {len(matched_df):,}")
print(f"  - ITEM_ONLY:           {rule_cnt.get('ITEM_ONLY',0):,}")
print(f"  - ITEM_OPTION_COMBO:   {rule_cnt.get('ITEM_OPTION_COMBO',0):,}")
print(f"  - ITEM_MULTI_COMBO:    {rule_cnt.get('ITEM_MULTI_OPTION_COMBO',0):,}")
print(f"  - MEAL_DEAL:           {rule_cnt.get('MEAL_DEAL',0):,}")
print(f"  - OPTION_COMPONENT:    {rule_cnt.get('OPTION_COMPONENT',0):,}")
print(f"  - STANDALONE_ADDON:    {rule_cnt.get('STANDALONE_ADDON',0):,}")
print(f"  Unmatched item rows:   {len(unmatched_items_df):,}")
print(f"  Unmatched option rows: {len(unmatched_opts_df):,}")
print(f"  Unique recipes:        {len(summary_df):,}")
