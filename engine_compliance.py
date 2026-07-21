"""
engine_compliance.py — System C
Merges System A (Bidfood stock) with System B (ingredient requirements)
and calculates the compliance gap per site per SKU.
"""

import re, difflib
import pandas as pd


def run(site_raw: pd.DataFrame, site_stock: pd.DataFrame) -> pd.DataFrame:
    """
    Merge ingredient requirements (B) vs Bidfood stock (A) and compute gap.

    Parameters
    ----------
    site_raw   : from engine_ingredient.run() — columns include Store, SKU, Total_Raw_Qty, UOM
    site_stock : from engine_bidfood.run()    — columns include Site_Key, Product Code,
                 Total_Ordered_Qty, Pack_UOM, Store_Name

    Returns
    -------
    compliance : DataFrame with columns:
                 Site_Key, Store_Name, SKU, Ingredient, Required_Qty, Req_UOM,
                 Ordered_Qty, Ord_UOM, Gap, Status
    """

    # ── Aggregate required qty per Store + SKU ───────────────────────────────
    # "Store" in site_raw = Items-wise Store column = same as mapping sheet
    # "Store Name" column, so it joins directly to ord_.Store_Name below.
    req = (
        site_raw
        .groupby(["Store","SKU","Ingredient","UOM"], dropna=False)
        ["Total_Raw_Qty"].sum()
        .reset_index()
        .rename(columns={
            "Store":         "Store_Name",   # Items Store == Bidfood mapping Store_Name
            "SKU":           "Product_Code",
            "Total_Raw_Qty": "Required_Qty",
            "UOM":           "Req_UOM",
        })
    )

    # ── Aggregate ordered qty per Site_Key + Store_Name + SKU ────────────────
    ord_ = (
        site_stock
        .groupby(["Site_Key","Store_Name","Product Code","Pack_UOM"], dropna=False)
        .agg(Ordered_Qty=("Total_Ordered_Qty","sum"))
        .reset_index()
        .rename(columns={
            "Product Code": "Product_Code",
            "Pack_UOM":     "Ord_UOM",
        })
    )

    # ── Merge on Store_Name + SKU (outer join) ────────────────────────────────
    # req.Store_Name  = "Fireaway Pizza (Norwich - Plumstead Rd)"
    # ord_.Store_Name = "Fireaway Pizza (Norwich - Plumstead Rd)"  ← same
    # ord_.Site_Key   = "Norwich - Plumstead Rd"                   ← location key
    compliance = req.merge(ord_, on=["Store_Name","Product_Code"], how="outer")

    # For ingredient-only rows (no Bidfood match) use Store_Name as fallback Site_Key
    compliance["Site_Key"] = compliance["Site_Key"].fillna(compliance["Store_Name"])

    compliance["Required_Qty"] = compliance["Required_Qty"].fillna(0)
    compliance["Ordered_Qty"]  = compliance["Ordered_Qty"].fillna(0)
    compliance["Gap"]          = compliance["Ordered_Qty"] - compliance["Required_Qty"]

    compliance["Status"] = compliance["Gap"].apply(
        lambda g: "Surplus" if g > 0 else ("Deficit" if g < 0 else "Exact")
    )

    # Friendly column names for dashboard
    compliance = compliance.rename(columns={"Product_Code":"SKU"})

    cols = [
        "Site_Key","Store_Name","SKU","Ingredient",
        "Required_Qty","Req_UOM",
        "Ordered_Qty","Ord_UOM",
        "Gap","Status",
    ]
    for c in cols:
        if c not in compliance.columns:
            compliance[c] = ""

    return compliance[cols].sort_values(["Site_Key","SKU"]).reset_index(drop=True)


def site_summary(compliance: pd.DataFrame) -> pd.DataFrame:
    """
    Summarise compliance at site level:
    counts of Surplus / Deficit / Exact SKUs per site.
    """
    grp = (
        compliance
        .groupby(["Site_Key","Store_Name","Status"])
        .agg(SKU_Count=("SKU","nunique"))
        .reset_index()
        .pivot_table(
            index=["Site_Key","Store_Name"],
            columns="Status",
            values="SKU_Count",
            fill_value=0,
        )
        .reset_index()
    )
    grp.columns.name = None
    for col in ["Surplus","Deficit","Exact"]:
        if col not in grp.columns:
            grp[col] = 0
    grp["Total_SKUs"] = grp["Surplus"] + grp["Deficit"] + grp["Exact"]
    grp["Compliance_%"] = (
        (grp["Surplus"] + grp["Exact"]) / grp["Total_SKUs"].replace(0, 1) * 100
    ).round(1)
    return grp.sort_values("Compliance_%", ascending=True)


def packaging_compliance(site_raw: pd.DataFrame, site_packaging: pd.DataFrame) -> pd.DataFrame:
    """
    Compute packaging compliance: Opalion orders vs Recipe Builder requirements.

    Parameters
    ----------
    site_raw      : from engine_ingredient.run() — ALL ingredient rows; packaging
                    rows are filtered here by Supplier containing 'opal'
    site_packaging: from engine_opalion.run()    — packaging ordered per site

    Returns
    -------
    pkg_compliance : DataFrame with columns:
                     Site_Key, SKU, Ingredient, Product_Name,
                     Required_Units, Ordered_Units, Gap, Status
    """

    COLS = ["Site_Key", "SKU", "Ingredient", "Product_Name",
            "Required_Units", "Ordered_Units", "Gap", "Status"]

    if site_raw.empty or site_packaging.empty:
        return pd.DataFrame(columns=COLS)

    # ── Filter site_raw to Opalion packaging rows ─────────────────────────────
    opal_mask = site_raw["Supplier"].str.lower().str.contains("opal", na=False)
    pkg_req_raw = site_raw[opal_mask].copy()

    if pkg_req_raw.empty:
        return pd.DataFrame(columns=COLS)

    pkg_req = (
        pkg_req_raw
        .groupby(["Store", "SKU", "Ingredient", "UOM"], dropna=False)
        ["Total_Raw_Qty"].sum()
        .reset_index()
        .rename(columns={"Store": "Store_Name", "Total_Raw_Qty": "Required_Units"})
    )

    # ── Fuzzy-match Ingredient -> Product_Base ────────────────────────────────
    # Recipe Builder ingredient names (e.g. "Hot Chick Grab Bag") should closely
    # match Opalion Product_Base names (e.g. "Hot Chick Grab Bag") after stripping
    # the "- Case of N" suffix that engine_opalion already removes.

    all_products  = site_packaging["Product_Base"].dropna().unique().tolist()
    norm_products = [re.sub(r'\s+', ' ', str(p)).strip().lower() for p in all_products]

    def _match_product(ingredient: str) -> str | None:
        if not ingredient:
            return None
        norm_ing = re.sub(r'\s+', ' ', str(ingredient)).strip().lower()
        # Exact
        if norm_ing in norm_products:
            return all_products[norm_products.index(norm_ing)]
        # Fuzzy (cutoff 0.65 — packaging names can differ slightly)
        hits = difflib.get_close_matches(norm_ing, norm_products, n=1, cutoff=0.65)
        if hits:
            return all_products[norm_products.index(hits[0])]
        return None

    pkg_req["Product_Base"] = pkg_req["Ingredient"].apply(_match_product)

    # ── Aggregate ordered units per Store_Name + Product_Base ────────────────
    ord_agg = (
        site_packaging
        .groupby(["Site_Key", "Store_Name", "Product_Base", "Product_Name"], dropna=False)
        .agg(Ordered_Units=("Total_Units", "sum"))
        .reset_index()
    ) if "Store_Name" in site_packaging.columns else (
        site_packaging
        .groupby(["Site_Key", "Product_Base", "Product_Name"], dropna=False)
        .agg(Ordered_Units=("Total_Units", "sum"))
        .reset_index()
        .assign(Store_Name=lambda d: d["Site_Key"])
    )

    # ── Outer merge on Store_Name + Product_Base ──────────────────────────────
    pkg_comp = pkg_req.merge(ord_agg, on=["Store_Name", "Product_Base"], how="outer")
    pkg_comp["Site_Key"] = pkg_comp.get("Site_Key", pkg_comp["Store_Name"])

    pkg_comp["Required_Units"] = pkg_comp["Required_Units"].fillna(0)
    pkg_comp["Ordered_Units"]  = pkg_comp["Ordered_Units"].fillna(0)
    pkg_comp["Gap"] = pkg_comp["Ordered_Units"] - pkg_comp["Required_Units"]
    pkg_comp["Status"] = pkg_comp["Gap"].apply(
        lambda g: "Surplus" if g > 0 else ("Deficit" if g < 0 else "Exact")
    )

    for c in COLS:
        if c not in pkg_comp.columns:
            pkg_comp[c] = ""

    return (
        pkg_comp[COLS]
        .sort_values(["Site_Key", "Ingredient"])
        .reset_index(drop=True)
    )


def packaging_site_summary(pkg_compliance: pd.DataFrame) -> pd.DataFrame:
    """
    Summarise packaging compliance at site level.
    Returns counts of Surplus / Deficit / Exact packaging items per site.
    """
    if pkg_compliance.empty:
        return pd.DataFrame(columns=["Site_Key","Surplus","Deficit","Exact",
                                     "Total_Items","Compliance_%"])

    grp = (
        pkg_compliance
        .groupby(["Site_Key", "Status"])
        .agg(Item_Count=("Ingredient", "nunique"))
        .reset_index()
        .pivot_table(
            index="Site_Key",
            columns="Status",
            values="Item_Count",
            fill_value=0,
        )
        .reset_index()
    )
    grp.columns.name = None
    for col in ["Surplus", "Deficit", "Exact"]:
        if col not in grp.columns:
            grp[col] = 0
    grp["Total_Items"]   = grp["Surplus"] + grp["Deficit"] + grp["Exact"]
    grp["Compliance_%"]  = (
        (grp["Surplus"] + grp["Exact"]) / grp["Total_Items"].replace(0, 1) * 100
    ).round(1)
    return grp.sort_values("Compliance_%", ascending=True)
