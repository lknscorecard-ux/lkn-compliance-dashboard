"""
engine_compliance.py — System C
Merges System A (Bidfood stock) with System B (ingredient requirements)
and calculates the compliance gap per site per SKU.
"""

import re, difflib
import pandas as pd


def run(
    site_raw: pd.DataFrame,
    site_stock: pd.DataFrame,
    store_site_map: dict | None = None,
    opening_stock: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Merge ingredient requirements (B) vs Bidfood stock (A) and compute gap.

    Parameters
    ----------
    site_raw       : from engine_ingredient.run() — columns include Store, SKU, Total_Raw_Qty, UOM
    site_stock     : from engine_bidfood.run()    — columns include Site_Key, Product Code,
                     Total_Ordered_Qty, Pack_UOM, Store_Name
    store_site_map : optional dict {Store_Name → Site_Key} from mapping sheet.
                     Used to resolve Site_Key for req-only rows that have no Bidfood
                     match and therefore no Site_Key from the Bidfood side.

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
        .agg(
            Ordered_Qty  =("Total_Ordered_Qty", "sum"),
            Bidfood_Desc =("Description",        "first"),  # carry SKU description
        )
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

    # Propagate Site_Key from matched rows to unmatched rows of the same Store_Name.
    # e.g. "Fireaway Pizza (Norwich - Plumstead Rd)" matched rows carry
    # Site_Key = "Norwich - Plumstead Rd"; unmatched rows for the same store
    # should use that same Site_Key instead of the Store_Name string.
    store_to_site = (
        compliance[compliance["Site_Key"].notna()]
        .groupby("Store_Name")["Site_Key"]
        .first()
        .to_dict()
    )
    compliance["Site_Key"] = compliance.apply(
        lambda r: store_to_site.get(r["Store_Name"], r["Store_Name"])
        if pd.isna(r["Site_Key"]) else r["Site_Key"],
        axis=1
    )

    # Fill blank Ingredient name from Bidfood description (Bidfood-only rows)
    compliance["Ingredient"] = compliance["Ingredient"].fillna(compliance.get("Bidfood_Desc", ""))
    if "Bidfood_Desc" in compliance.columns:
        blank = compliance["Ingredient"].isna() | (compliance["Ingredient"] == "")
        compliance.loc[blank, "Ingredient"] = compliance.loc[blank, "Bidfood_Desc"]
        compliance = compliance.drop(columns=["Bidfood_Desc"])

    compliance["Required_Qty"] = compliance["Required_Qty"].fillna(0)
    compliance["Ordered_Qty"]  = compliance["Ordered_Qty"].fillna(0)

    # Normalise UOMs to grams / ml (1 ml ≈ 1 g for liquids) before computing gap
    # so that e.g. an order in Litres is not compared raw against a Gram requirement.
    _UOM_TO_G = {
        "gram": 1, "grams": 1, "g": 1,
        "ml": 1,                                              # 1 ml ≈ 1 g
        "l": 1000, "litre": 1000, "litres": 1000,
        "liter": 1000, "liters": 1000,
        "kg": 1000, "kilogram": 1000, "kilograms": 1000,
        "each": 1, "unit": 1, "units": 1,
        "pcs": 1, "piece": 1, "pieces": 1,
    }
    _req_norm = (
        compliance["Required_Qty"]
        * compliance["Req_UOM"].fillna("").str.strip().str.lower().map(_UOM_TO_G).fillna(1)
    )
    _ord_norm = (
        compliance["Ordered_Qty"]
        * compliance["Ord_UOM"].fillna("").str.strip().str.lower().map(_UOM_TO_G).fillna(1)
    )
    compliance["Gap"] = _ord_norm - _req_norm

    compliance["Status"] = compliance["Gap"].apply(
        lambda g: "Surplus" if g > 0 else ("Deficit" if g < 0 else "Exact")
    )

    # Final Site_Key resolution for req-only rows (no Bidfood match at all).
    # After propagation, any row where Site_Key still equals Store_Name is a
    # store that exists in the Items-wise data but never ordered through an LKN
    # Bidfood account. Use the mapping sheet lookup (if provided) to set the
    # correct location key instead of leaving the full store name.
    if store_site_map:
        _is_fallback = compliance["Site_Key"] == compliance["Store_Name"]
        compliance.loc[_is_fallback, "Site_Key"] = (
            compliance.loc[_is_fallback, "Store_Name"]
            .map(store_site_map)
            .fillna(compliance.loc[_is_fallback, "Store_Name"])
        )

    # Friendly column names for dashboard
    compliance = compliance.rename(columns={"Product_Code":"SKU"})

    # ── Rolling inventory: opening stock carry-forward ────────────────────────
    # opening_stock: DataFrame with columns [Site_Key, SKU, Closing_Stock_g]
    # from the previous week's run. If absent (first run), defaults to 0.
    compliance["Opening_Stock_g"] = 0.0
    if opening_stock is not None and not opening_stock.empty:
        # Normalise keys on BOTH sides: strip whitespace, cast to str.
        # This prevents "6583" vs "06583" or trailing-space mismatches.
        _os = opening_stock.copy()
        _os["Site_Key"] = _os["Site_Key"].astype(str).str.strip()
        _os["SKU"]      = _os["SKU"].astype(str).str.strip()
        _os_map = (
            _os.groupby(["Site_Key", "SKU"])["Closing_Stock_g"]
            .sum()  # sum in case of duplicates
            .to_dict()
        )
        compliance["Opening_Stock_g"] = compliance.apply(
            lambda r: float(_os_map.get(
                (str(r["Site_Key"]).strip(), str(r["SKU"]).strip()), 0.0
            )),
            axis=1,
        )
        _matched = int((compliance["Opening_Stock_g"] > 0).sum())
        import logging as _log
        _log.getLogger(__name__).info(
            "  Opening stock applied to %d rows (non-zero)", _matched
        )

    # Recompute Gap = (Ordered + Opening) − Required  (all in normalised g/ml)
    compliance["Gap"] = (_ord_norm + compliance["Opening_Stock_g"]) - _req_norm
    compliance["Status"] = compliance["Gap"].apply(
        lambda g: "Surplus" if g > 0 else ("Deficit" if g < 0 else "Exact")
    )
    # Closing stock = stock remaining after this week's consumption (≥ 0)
    compliance["Closing_Stock_g"] = compliance["Gap"].clip(lower=0).round(1)

    cols = [
        "Site_Key","Store_Name","SKU","Ingredient",
        "Required_Qty","Req_UOM",
        "Ordered_Qty","Ord_UOM",
        "Opening_Stock_g","Gap","Closing_Stock_g","Status",
    ]
    for c in cols:
        if c not in compliance.columns:
            compliance[c] = ""

    return compliance[cols].sort_values(["Site_Key","SKU"]).reset_index(drop=True)


TOTAL_TRACKED_SKUS = 16  # fallback denominator if no recipe requirements found

def site_summary(compliance: pd.DataFrame) -> pd.DataFrame:
    """
    Summarise compliance at site level.
    Compliance % = Compliant SKUs / Required SKUs * 100
    Required SKUs = distinct SKUs with Required_Qty > 0 at this site
    (falls back to TOTAL_TRACKED_SKUS if none found).
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
    grp["Total_SKUs"]   = grp["Surplus"] + grp["Deficit"] + grp["Exact"]
    # Compliance % = Compliant SKUs / Total SKUs * 100
    grp["Compliance_%"] = (
        (grp["Surplus"] + grp["Exact"])
        / grp["Total_SKUs"].replace(0, float("nan")) * 100
    ).round(1).fillna(0)
    return grp.sort_values("Compliance_%", ascending=True)


def packaging_compliance(
    site_raw: pd.DataFrame,
    site_packaging: pd.DataFrame,
    store_site_map: dict | None = None,
    opening_stock: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Compute packaging compliance: Opalion orders vs Recipe Builder requirements.
    Joins on Site_Key + SKU (both sides keyed by SKU from the mapping tab).

    Parameters
    ----------
    site_raw      : from engine_ingredient.run() — packaging rows have Supplier='Opalion'
    site_packaging: from engine_opalion.run()    — columns: Site_Key, SKU, Total_Units
    store_site_map: optional {Store_Name -> Site_Key} to resolve req-side store names

    Returns
    -------
    pkg_compliance : DataFrame — Site_Key, SKU, Ingredient, Product_Name,
                                 Required_Units, Ordered_Units, Gap, Status
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    COLS = ["Site_Key", "SKU", "Ingredient", "Product_Name",
            "Opening_Units", "Required_Units", "Ordered_Units",
            "Gap", "Closing_Units", "Status"]

    if site_raw.empty or site_packaging.empty:
        return pd.DataFrame(columns=COLS)

    # ── Filter site_raw to Opalion packaging rows ─────────────────────────────
    if "Supplier" not in site_raw.columns:
        return pd.DataFrame(columns=COLS)
    opal_mask   = site_raw["Supplier"].str.lower().str.contains("opal", na=False)
    pkg_req_raw = site_raw[opal_mask].copy()

    if pkg_req_raw.empty:
        _logger.warning("  packaging_compliance: no Opalion rows in site_raw (check Supplier column)")
        return pd.DataFrame(columns=COLS)

    # Aggregate required units per Store + SKU
    pkg_req = (
        pkg_req_raw
        .groupby(["Store", "SKU", "Ingredient"], dropna=False)
        .agg(Required_Units=("Total_Raw_Qty", "sum"))
        .reset_index()
        .rename(columns={"Store": "Store_Name"})
    )

    # Resolve Store_Name → Site_Key
    pkg_req["Site_Key"] = pkg_req["Store_Name"].map(store_site_map or {})
    pkg_req["Site_Key"] = pkg_req["Site_Key"].fillna(pkg_req["Store_Name"])

    req_agg = (
        pkg_req
        .groupby(["Site_Key", "SKU", "Ingredient"], dropna=False)
        .agg(Required_Units=("Required_Units", "sum"))
        .reset_index()
    )
    _logger.info("  packaging_compliance: %d req rows | %d sites",
                 len(req_agg), req_agg["Site_Key"].nunique())

    # ── Aggregate ordered units per Site_Key + SKU ────────────────────────────
    _agg_dict = {"Ordered_Units": ("Total_Units", "sum")}
    if "Total_Cases" in site_packaging.columns:
        _agg_dict["Ordered_Cases"] = ("Total_Cases", "sum")
    ord_agg = (
        site_packaging
        .groupby(["Site_Key", "SKU", "Recipe_Name", "Product_Name"], dropna=False)
        .agg(**_agg_dict)
        .reset_index()
    )

    # ── Outer merge on Site_Key + SKU ─────────────────────────────────────────
    pkg_comp = req_agg.merge(ord_agg, on=["Site_Key", "SKU"], how="outer")

    pkg_comp["Required_Units"] = pd.to_numeric(pkg_comp["Required_Units"], errors="coerce").fillna(0)
    pkg_comp["Ordered_Units"]  = pd.to_numeric(pkg_comp["Ordered_Units"],  errors="coerce").fillna(0)

    # ── Opening stock carry-forward (previous week's closing units) ───────────
    pkg_comp["Opening_Units"] = 0.0
    if opening_stock is not None and not opening_stock.empty:
        _os = opening_stock.copy()
        _os["Site_Key"] = _os["Site_Key"].astype(str).str.strip()
        _os["SKU"]      = _os["SKU"].astype(str).str.strip()
        _os_map = _os.groupby(["Site_Key", "SKU"])["Closing_Units"].sum().to_dict()
        pkg_comp["Opening_Units"] = pkg_comp.apply(
            lambda r: float(_os_map.get(
                (str(r["Site_Key"]).strip(), str(r["SKU"]).strip()), 0.0
            )),
            axis=1,
        )
        _matched = int((pkg_comp["Opening_Units"] > 0).sum())
        _logger.info("  Packaging opening stock applied to %d rows", _matched)

    # Gap = (Ordered + Opening) − Required
    pkg_comp["Gap"] = (
        pkg_comp["Ordered_Units"] + pkg_comp["Opening_Units"]
    ) - pkg_comp["Required_Units"]

    # Closing stock = surplus units carried to next week (≥ 0)
    pkg_comp["Closing_Units"] = pkg_comp["Gap"].clip(lower=0).round(0)

    # Compliant if gap ≥ 0 (enough stock ordered + carry-forward)
    pkg_comp["Status"] = pkg_comp["Gap"].apply(
        lambda g: "Compliant" if g >= 0 else "Non-Compliant"
    )

    # Cases columns — derive units-per-case from Ordered_Units / Ordered_Cases
    if "Ordered_Cases" in pkg_comp.columns:
        pkg_comp["Ordered_Cases"] = pd.to_numeric(pkg_comp["Ordered_Cases"], errors="coerce").fillna(0)
        _u = pkg_comp["Ordered_Units"]
        _c = pkg_comp["Ordered_Cases"]
        _upc = (_u / _c).where(_c > 0)  # units per case
        pkg_comp["Gap_Cases"] = (pkg_comp["Gap"] / _upc).round(2)
        pkg_comp["Ordered_Cases"] = pkg_comp["Ordered_Cases"].round(2)

    # Fill display columns
    if "Ingredient"   not in pkg_comp.columns: pkg_comp["Ingredient"]   = ""
    if "Product_Name" not in pkg_comp.columns: pkg_comp["Product_Name"] = ""
    pkg_comp["Ingredient"]   = pkg_comp["Ingredient"].fillna(pkg_comp.get("Recipe_Name", ""))
    pkg_comp["Product_Name"] = pkg_comp["Product_Name"].fillna("")

    COLS_OUT = COLS + (
        ["Ordered_Cases", "Gap_Cases"]
        if "Ordered_Cases" in pkg_comp.columns else []
    )
    for c in COLS_OUT:
        if c not in pkg_comp.columns:
            pkg_comp[c] = ""

    _logger.info(
        "  packaging_compliance: %d rows | Compliant %d | Non-Compliant %d",
        len(pkg_comp),
        int((pkg_comp["Status"] == "Compliant").sum()),
        int((pkg_comp["Status"] == "Non-Compliant").sum()),
    )

    return pkg_comp[COLS_OUT].sort_values(["Site_Key", "SKU"]).reset_index(drop=True)


def packaging_site_summary(pkg_compliance: pd.DataFrame) -> pd.DataFrame:
    """Summarise packaging compliance at site level."""
    if pkg_compliance.empty:
        return pd.DataFrame(columns=["Site_Key", "Compliant", "Non-Compliant",
                                     "Total_Items", "Compliance_%"])

    grp = (
        pkg_compliance
        .groupby(["Site_Key", "Status"])
        .agg(Item_Count=("SKU", "nunique"))
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
    for col in ["Compliant", "Non-Compliant"]:
        if col not in grp.columns:
            grp[col] = 0
    grp["Total_Items"]  = grp["Compliant"] + grp["Non-Compliant"]
    grp["Compliance_%"] = (
        grp["Compliant"] / grp["Total_Items"].replace(0, 1) * 100
    ).round(1)
    return grp.sort_values("Compliance_%", ascending=True)
