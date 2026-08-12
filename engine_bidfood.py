"""
engine_bidfood.py — System A
Maps Bidfood order data to LKN sites and aggregates ordered stock quantities.
"""

import re, warnings
import pandas as pd
warnings.filterwarnings("ignore")


# ── Pack size parser ───────────────────────────────────────────────────────────

def parse_pack_size(raw):
    """Return (qty_per_case, uom) where uom in {'g','ml','each'}."""
    if not raw or str(raw).strip() == "":
        return None, None
    s = str(raw).strip().lower().replace(" ", "")

    # Three-part with unit: {N}-{A}x{B}{unit}  e.g. '4-30x56g'
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(kg|g|ltr|l|ml)?$", s)
    if m:
        outer, inner, amount = float(m.group(1)), float(m.group(2)), float(m.group(3))
        unit = m.group(4) or "g"
        total = outer * inner * amount
        if unit == "kg":          return total * 1000, "g"
        elif unit in ("ltr","l"): return total * 1000, "ml"
        elif unit == "ml":        return total, "ml"
        else:                     return total, "g"

    # Three-part no unit: {N}x{A}x{B}
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$", s)
    if m:
        outer, inner, amount = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return outer * inner * amount, "g"

    # Weight range midpoint: {N}-{A}-{B}{unit}  e.g. '20-140-170'
    m = re.match(r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)(g|kg|ml)?$", s)
    if m:
        count = float(m.group(1))
        lo, hi = float(m.group(2)), float(m.group(3))
        unit = m.group(4) or "g"
        mid = (lo + hi) / 2
        total = count * mid
        if unit == "kg":  return total * 1000, "g"
        elif unit == "ml": return total, "ml"
        else:              return total, "g"

    # Two-part: {N}-{A}{unit}
    m = re.match(
        r"^(\d+(?:\.\d+)?)[x\-](\d+(?:\.\d+)?)"
        r"(kg|g|ltr|l|ml|gpk|ea|each|bun|pk|ptn|pt|box|ca|roll|m)?$",
        s, re.I
    )
    if m:
        count, amount = float(m.group(1)), float(m.group(2))
        unit = (m.group(3) or "g").lower()
        if unit == "kg":              return count * amount * 1000, "g"
        elif unit in ("ltr","l"):     return count * amount * 1000, "ml"
        elif unit == "ml":            return count * amount, "ml"
        elif unit in ("g","gpk"):     return count * amount, "g"
        else:                         return count * amount, "each"

    return None, None


# ── Main engine function ───────────────────────────────────────────────────────

def run(bf_df: pd.DataFrame, mapping_df: pd.DataFrame) -> tuple:
    """
    Map Bidfood orders to LKN sites and aggregate ordered quantities.

    Parameters
    ----------
    bf_df      : Bidfood order export DataFrame
    mapping_df : Drop Account Mapping DataFrame (from gsheet_client or local Excel)

    Returns
    -------
    site_stock  : DataFrame — stock ordered per Site × SKU
    sku_summary : DataFrame — stock summary per SKU (all sites)
    bf_lkn      : DataFrame — raw Bidfood rows matched to LKN sites
    unmatched   : DataFrame — Bidfood rows with no site match
    """

    # Clean Bidfood DF
    bf = bf_df.copy()
    bf.columns = bf.columns.str.strip()
    for col in ["Customer Code","Product Code","Description","Brand",
                "Pack Size","Unit of Measure","Order Date","Delivery Date","Account Name"]:
        if col in bf.columns:
            bf[col] = bf[col].fillna("").astype(str).str.strip()
    bf["Quantity Ordered"] = pd.to_numeric(bf.get("Quantity Ordered",""), errors="coerce").fillna(0)
    bf["Unit Price"]       = pd.to_numeric(bf.get("Unit Price",""),       errors="coerce").fillna(0)
    bf["Total"]            = pd.to_numeric(bf.get("Total",""),            errors="coerce").fillna(0)

    # Clean mapping DF
    mp = mapping_df.copy()
    mp.columns = mp.columns.str.strip()
    # Find account-number column regardless of exact name
    _acct_col = next(
        (c for c in mp.columns
         if "drop account" in c.lower() or ("account" in c.lower() and "number" in c.lower())),
        None
    )
    if _acct_col is None:
        raise KeyError(f"Cannot find Drop Account Number column. Available columns: {mp.columns.tolist()}")
    mp[_acct_col] = mp[_acct_col].fillna("").astype(str).str.strip()
    mp["Site Key"]   = mp["Site Key"].fillna("").astype(str).str.strip()
    mp["Store Name"] = mp["Store Name"].fillna("").astype(str).str.strip()

    acc_to_site  = dict(zip(mp[_acct_col], mp["Site Key"]))
    acc_to_store = dict(zip(mp[_acct_col], mp["Store Name"]))

    bf["Site_Key"]   = bf["Customer Code"].map(acc_to_site).fillna("")
    bf["Store_Name"] = bf["Customer Code"].map(acc_to_store).fillna("")

    matched_mask = bf["Site_Key"] != ""
    bf_lkn  = bf[matched_mask].copy()
    bf_other = bf[~matched_mask].copy()

    # Parse pack sizes
    parsed = bf_lkn["Pack Size"].apply(parse_pack_size).tolist()
    bf_lkn[["Pack_Qty","Pack_UOM"]] = pd.DataFrame(parsed, index=bf_lkn.index)
    bf_lkn["Pack_Qty"] = pd.to_numeric(bf_lkn["Pack_Qty"], errors="coerce")
    bf_lkn["Total_Ordered_Qty"] = bf_lkn["Quantity Ordered"] * bf_lkn["Pack_Qty"]
    bf_lkn["Total_Spend_GBP"]   = bf_lkn["Total"]

    # Site stock: aggregate per Site × SKU
    site_stock = (
        bf_lkn
        .groupby(["Site_Key","Store_Name","Product Code","Description","Brand",
                  "Pack Size","Pack_UOM","Unit of Measure"], dropna=False)
        .agg(
            Cases_Ordered    =("Quantity Ordered",  "sum"),
            Total_Ordered_Qty=("Total_Ordered_Qty", "sum"),
            Pack_Qty         =("Pack_Qty",          "first"),  # g per case (same for all rows with same Pack Size)
            Total_Spend_GBP  =("Total_Spend_GBP",   "sum"),
            Unit_Price       =("Unit Price",         "median"),  # actual invoice price per case
        )
        .reset_index()
        .sort_values(["Site_Key","Product Code"])
    )

    # SKU summary: aggregate across all sites
    sku_summary = (
        bf_lkn
        .groupby(["Product Code","Description","Brand","Pack Size","Pack_UOM"], dropna=False)
        .agg(
            Cases_Ordered    =("Quantity Ordered",  "sum"),
            Total_Ordered_Qty=("Total_Ordered_Qty", "sum"),
            Total_Spend_GBP  =("Total_Spend_GBP",   "sum"),
            Sites_Ordering   =("Site_Key",           "nunique"),
        )
        .reset_index()
        .sort_values("Total_Ordered_Qty", ascending=False)
    )

    return site_stock, sku_summary, bf_lkn, bf_other
