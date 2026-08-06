"""
engine_opalion.py — Packaging Stock Engine (v2)
================================================
Processes the Opalion 'Line Item Details by Company' CSV report.

Site matching : Order Billing Zip Code  →  Opalion code (Site Mapping)  →  Site Key
SKU matching  : Product name from Opalion  →  Opalion SKU Mapping 2 tab  →  SKU + Cases

CSV columns expected:
    Product name, Net quantity, Order Billing Zip Code
"""

import logging
import pandas as pd

log = logging.getLogger(__name__)


def run(
    opalion_df: pd.DataFrame,
    site_mapping_df: pd.DataFrame,
    sku_mapping_df: pd.DataFrame,
) -> tuple:
    """
    Process Opalion packaging order report.

    Parameters
    ----------
    opalion_df      : Opalion 'Line Item Details' CSV as DataFrame
    site_mapping_df : Site Mapping sheet — needs 'Opalion code' + 'Site Key' columns
    sku_mapping_df  : 'Opalion SKU Mapping 2' tab — columns:
                      'Product name from Opalion', 'Recipe name', 'SKU', 'Cases'

    Returns
    -------
    site_packaging  : DataFrame — packaging ordered per Site_Key + SKU
    pkg_sku_summary : DataFrame — totals per SKU across all sites
    unmatched       : DataFrame — rows with no site or SKU match
    """

    df = opalion_df.copy()
    df.columns = df.columns.str.strip()

    # ── Standardise column names ──────────────────────────────────────────────
    rename = {
        "Product name":           "Product_Name",
        "Net quantity":           "Net_Qty",
        "Order Billing Zip Code": "Zip_Code",
        "Order Billing Company":  "Company",
        "Customer name":          "Customer_Name",
        "Order name":             "Order_Name",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["Product_Name", "Zip_Code"]:
        if col not in df.columns:
            raise ValueError(f"Opalion report missing required column: '{col}'")

    df["Net_Qty"]      = pd.to_numeric(df.get("Net_Qty", 0), errors="coerce").fillna(0)
    df["Product_Name"] = df["Product_Name"].fillna("").astype(str).str.strip()
    df["Zip_Code"]     = df["Zip_Code"].fillna("").astype(str).str.strip().str.upper()

    # ── Build zip code → Site_Key / Store_Name lookup ────────────────────────
    sm = site_mapping_df.copy()
    sm.columns = sm.columns.str.strip()
    if "Site Key" in sm.columns and "Site_Key" not in sm.columns:
        sm = sm.rename(columns={"Site Key": "Site_Key"})

    zip_to_site:  dict[str, str] = {}
    zip_to_store: dict[str, str] = {}

    if "Opalion code" in sm.columns and "Site_Key" in sm.columns:
        for _, row in sm.iterrows():
            zc = str(row["Opalion code"]).strip().upper()
            sk = str(row["Site_Key"]).strip()
            sn = str(row.get("Store Name", row.get("Store_Name", ""))).strip()
            if zc and zc not in ("", "NAN", "#N/A") and sk:
                zip_to_site[zc]  = sk
                zip_to_store[zc] = sn
        log.info("  Opalion site lookup: %d zip codes mapped", len(zip_to_site))
    else:
        log.warning("  Site Mapping missing 'Opalion code' or 'Site_Key' column — no sites will match")

    df["Site_Key"]   = df["Zip_Code"].map(zip_to_site)
    df["Store_Name"] = df["Zip_Code"].map(zip_to_store)

    # ── Build product name → SKU + Cases lookup ───────────────────────────────
    skm = sku_mapping_df.copy()
    skm.columns = skm.columns.str.strip()

    _prod_col   = next((c for c in skm.columns if "product name" in c.lower()), None)
    _sku_col    = next((c for c in skm.columns if c.strip().upper() == "SKU"), None)
    _case_col   = next((c for c in skm.columns if c.strip().lower() == "cases"), None)
    _recipe_col = next((c for c in skm.columns if "recipe" in c.lower() and "name" in c.lower()), None)

    prod_to_sku:    dict[str, str]   = {}
    prod_to_cases:  dict[str, float] = {}
    prod_to_recipe: dict[str, str]   = {}

    if _prod_col and _sku_col and _case_col:
        for _, row in skm.iterrows():
            pname  = str(row[_prod_col]).strip()
            sku    = str(row[_sku_col]).strip()
            cases  = row[_case_col]
            recipe = str(row[_recipe_col]).strip() if _recipe_col else ""
            # Skip rows with no SKU mapping or invalid case count
            if not pname or sku in ("", "None", "nan", "#N/A"):
                continue
            try:
                cases_n = float(str(cases).replace(",", ""))
                if cases_n <= 0:
                    continue
            except (ValueError, TypeError):
                continue
            prod_to_sku[pname]    = sku
            prod_to_cases[pname]  = cases_n
            prod_to_recipe[pname] = recipe
        log.info("  Opalion SKU lookup: %d products mapped", len(prod_to_sku))
    else:
        log.warning(
            "  SKU Mapping 2 missing expected columns "
            "(found: %s) — no packaging SKUs will match", list(skm.columns)
        )

    df["SKU"]         = df["Product_Name"].map(prod_to_sku)
    df["Cases"]       = df["Product_Name"].map(prod_to_cases)
    df["Recipe_Name"] = df["Product_Name"].map(prod_to_recipe)

    # ── Split matched / unmatched ─────────────────────────────────────────────
    no_site = df[df["Site_Key"].isna()].copy()
    no_sku  = df[df["Site_Key"].notna() & df["SKU"].isna()].copy()
    unmatched = pd.concat([no_site, no_sku], ignore_index=True)

    log.info(
        "  Opalion rows: %d total | %d site-unmatched | %d sku-unmatched",
        len(df), len(no_site), len(no_sku),
    )

    matched = df[df["Site_Key"].notna() & df["SKU"].notna()].copy()
    matched["Total_Units"] = matched["Net_Qty"] * matched["Cases"]

    # ── Aggregate: one row per Site_Key + SKU ─────────────────────────────────
    site_packaging = (
        matched
        .groupby(
            ["Site_Key", "Store_Name", "SKU", "Recipe_Name", "Product_Name"],
            dropna=False,
        )
        .agg(
            Total_Cases    =("Net_Qty",      "sum"),
            Cases_Per_Unit =("Cases",         "first"),
            Total_Units    =("Total_Units",  "sum"),
        )
        .reset_index()
        .sort_values(["Site_Key", "SKU"])
        .reset_index(drop=True)
    )

    # ── Summary: totals per SKU across all sites ──────────────────────────────
    pkg_sku_summary = (
        matched
        .groupby(["SKU", "Recipe_Name", "Product_Name"], dropna=False)
        .agg(
            Total_Cases   =("Net_Qty",     "sum"),
            Total_Units   =("Total_Units", "sum"),
            Sites_Ordered =("Site_Key",    "nunique"),
        )
        .reset_index()
        .sort_values("Total_Units", ascending=False)
        .reset_index(drop=True)
    )

    log.info(
        "  Opalion matched: %d site×SKU rows across %d sites",
        len(site_packaging),
        site_packaging["Site_Key"].nunique() if not site_packaging.empty else 0,
    )

    return site_packaging, pkg_sku_summary, unmatched
