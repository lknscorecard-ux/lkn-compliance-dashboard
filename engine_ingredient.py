"""
engine_ingredient.py — System B Part 2
Computes per-site ingredient quantities from matched sales + Recipe Builder.
"""

import re, difflib, warnings
import pandas as pd
warnings.filterwarnings("ignore")

_DRINK_KW = ["coke","pepsi","sprite","fanta","7 up","7up","tango orange",
             "water can","sparkling can","still water"]

_SAUCE_MENU_ITEMS = [
    "Honey Buffalo Sauce","Korean Sauce","Ranch Sauce",
    "BBQ Sauce","Chilli Mayo Sauce"
]

_SAUCE_KW = [
    (["buffalo","honey buffalo","extra hot honey","extra hot"],  "Honey Buffalo Sauce"),
    (["bbq","smokey","smoky","smoked","burning"],                "BBQ Sauce"),
    (["korean","seoul","killer hot korean"],                      "Korean Sauce"),
    (["ranch","creamy ranch","original house ranch"],             "Ranch Sauce"),
    (["sriracha","chilli mayo","mayo","scorching"],               "Chilli Mayo Sauce"),
]

BRAND_SHEETS = {
    "Hot Chick":  "Hot Chick",
    "WTF":        "WTF",
    "Korea Town": "Koreatown",
    "Wing Fest":  "Wing Fest",
    "Kuro Smash": "Kurosmash",
}


def _resolve_sauce_name(sauce_str):
    if not sauce_str or str(sauce_str).lower() in ("nan","none",""):
        return None
    s = sauce_str.lower()
    for kws, menu_name in _SAUCE_KW:
        if any(kw in s for kw in kws):
            return menu_name
    return None


def _normalize(s):
    return re.sub(r'\s+', ' ', str(s)).strip().lower()


def _extract_size(s):
    m = re.search(r'\((\d+)\)', s)
    if m: return int(m.group(1))
    m = re.search(r'[-\s](\d+)\s*$', s)
    if m: return int(m.group(1))
    return None


def _build_fuzzy_resolver(rb):
    all_names     = [n for n in rb["Menu Item Name"].unique() if n]
    norm_to_rb    = {_normalize(n): n for n in all_names}
    norm_rb_list  = list(norm_to_rb.keys())

    def fuzzy_resolve(recipe_item):
        norm = _normalize(recipe_item)
        if norm in norm_to_rb:
            return norm_to_rb[norm], "normalized_exact", None
        ri_size  = _extract_size(recipe_item)
        matches  = difflib.get_close_matches(norm, norm_rb_list, n=5, cutoff=0.78)
        if matches:
            if ri_size is not None:
                for m in matches:
                    if _extract_size(norm_to_rb[m]) == ri_size:
                        return norm_to_rb[m], "fuzzy_same_size", None
            return norm_to_rb[matches[0]], "fuzzy", None
        # Strip sauce suffix
        parts = recipe_item.rsplit(' - ', 1)
        if len(parts) == 2 and parts[1].strip() and not parts[1].strip().isdigit():
            base, sauce = parts[0].strip(), parts[1].strip()
            base_norm   = _normalize(base)
            if base_norm in norm_to_rb:
                return norm_to_rb[base_norm], "sauce_stripped", sauce
            base_size    = _extract_size(base)
            base_matches = difflib.get_close_matches(base_norm, norm_rb_list, n=3, cutoff=0.78)
            if base_matches:
                if base_size is not None:
                    for m in base_matches:
                        if _extract_size(norm_to_rb[m]) == base_size:
                            return norm_to_rb[m], "sauce_stripped_fuzzy", sauce
                return norm_to_rb[base_matches[0]], "sauce_stripped_fuzzy", sauce
        return None, "unmatched", None

    return fuzzy_resolve


def run(matched_df: pd.DataFrame, rb_xl_path: str) -> tuple:
    """
    Compute ingredient requirements from matched sales and Recipe Builder.

    Parameters
    ----------
    matched_df  : output of engine_recipe.run() — Matched Sales
    rb_xl_path  : path to 'Recipe builder.xlsx'

    Returns
    -------
    site_raw           : DataFrame — ingredient qty per Brand+Store+SKU
    raw_summary        : DataFrame — ingredient qty per Brand+SKU
    ingredient_summary : DataFrame — ingredient qty across all brands
    unmatched_report   : DataFrame — items with no recipe match
    """

    # ── Load matched sales ────────────────────────────────────────────────────
    ms = matched_df.copy()
    ms.columns = ms.columns.str.strip()
    ms["Quantity"]    = pd.to_numeric(ms["Quantity"],    errors="coerce").fillna(0)
    ms["Item_id"]     = ms.get("Item_id",    pd.Series([""] * len(ms))).fillna("").astype(str).str.strip()
    ms["Option_id_1"] = ms.get("Option_id_1",pd.Series([""] * len(ms))).fillna("").astype(str).str.strip()
    ms["Option_id_2"] = ms.get("Option_id_2",pd.Series([""] * len(ms))).fillna("").astype(str).str.strip()
    ms["Store"]       = ms.get("Store",  pd.Series(["Unknown"] * len(ms))).fillna("Unknown").astype(str).str.strip()
    ms["Brand"]       = ms.get("Brand",  pd.Series([""] * len(ms))).fillna("").astype(str).str.strip()
    ms["Recipe_Item"] = ms.get("Recipe_Item", pd.Series([""] * len(ms))).fillna("").astype(str).str.strip()

    # Exclude drinks
    is_drink = ms["Recipe_Item"].apply(lambda n: any(kw in n.lower() for kw in _DRINK_KW))
    ms = ms[~is_drink].copy()

    sales = (ms.groupby(["Brand","Store","Recipe_Item","Item_id","Option_id_1"], dropna=False)
               ["Quantity"].sum().reset_index())

    # ── Load Recipe Builder ───────────────────────────────────────────────────
    rb_xl  = pd.ExcelFile(rb_xl_path, engine="calamine")
    rb_plu = rb_xl.parse("PLU Mapping", dtype=str)
    rb_plu.columns = rb_plu.columns.str.strip()
    rb_plu = rb_plu.dropna(subset=["Menu Item Name"])
    rb_plu["Item_id"]   = rb_plu["Item_id"].fillna("").str.strip()
    rb_plu["Option_id"] = rb_plu["Option_id"].fillna("").str.strip()
    rb_plu["Menu Item Name"] = rb_plu["Menu Item Name"].str.strip()

    item_id_to_name   = dict(zip(rb_plu["Item_id"],   rb_plu["Menu Item Name"]))
    option_id_to_name = dict(zip(rb_plu["Option_id"], rb_plu["Menu Item Name"]))

    rb_frames = []
    for brand, sheet in BRAND_SHEETS.items():
        try:
            df = rb_xl.parse(sheet, dtype=str)
            df.columns = df.columns.str.strip()
            if "Item_ID" in df.columns:
                df = df.rename(columns={"Item_ID":"Item_id"})
            df["_brand"] = brand
            rb_frames.append(df)
        except Exception:
            pass

    rb = pd.concat(rb_frames, ignore_index=True)
    rb.columns = rb.columns.str.strip()
    rb["Menu Item Name"] = rb["Menu Item Name"].fillna("").str.strip()
    rb["Ingredient"]     = rb["Ingredient"].fillna("").str.strip()
    rb["Qty_new"]        = pd.to_numeric(rb["Qty_new"],  errors="coerce").fillna(0)
    rb["Cost"]           = pd.to_numeric(rb["Cost"],     errors="coerce").fillna(0)
    rb["UOM_new"]        = rb["UOM_new"].fillna("").str.strip()
    rb["SKU Code"]       = rb["SKU Code"].fillna("").str.strip()
    rb["Supplier"]       = rb["Supplier"].fillna("").str.strip()
    rb["Storage Type"]   = rb["Storage Type"].fillna("").str.strip()

    rb = rb[rb["Ingredient"] != ""].copy()
    # Packaging (Opalion) rows are intentionally included

    # Sauce lookup
    sauce_rb_raw = rb[rb["Menu Item Name"].isin(_SAUCE_MENU_ITEMS)].copy()
    sauce_lookup = {}
    for smi in _SAUCE_MENU_ITEMS:
        rows = sauce_rb_raw[sauce_rb_raw["Menu Item Name"] == smi]
        food = rows[~rows["Supplier"].str.lower().str.contains("opal", na=False)]
        if not food.empty:
            r = food.iloc[0]
            sauce_lookup[smi] = {
                "Ingredient": r["Ingredient"], "SKU Code": r["SKU Code"],
                "UOM_new": r["UOM_new"], "Supplier": r["Supplier"],
                "Storage Type": r["Storage Type"], "Cost": r["Cost"],
            }

    # ── Resolve Recipe_Item -> Menu Item Name ─────────────────────────────────
    fuzzy_resolve = _build_fuzzy_resolver(rb)
    all_names_set = set(rb["Menu Item Name"])

    def resolve(row):
        if row["Item_id"]:
            n = item_id_to_name.get(row["Item_id"])
            if n: return n, "item_id", None
        if row["Option_id_1"]:
            n = option_id_to_name.get(row["Option_id_1"])
            if n: return n, "option_id", None
        ri = row["Recipe_Item"]
        if ri in all_names_set:
            return ri, "direct_name", None
        return fuzzy_resolve(ri)

    results = sales.copy()
    results[["Menu_Item_Name","Match_Method","Sauce_Name"]] = results.apply(
        lambda r: pd.Series(resolve(r)), axis=1
    )

    matched_sales   = results[results["Menu_Item_Name"].notna()].copy()
    unmatched_sales = results[results["Menu_Item_Name"].isna()].copy()

    # ── Join with Recipe Builder ingredients ──────────────────────────────────
    detail = matched_sales.merge(
        rb[["Menu Item Name","Ingredient","Qty_new","UOM_new","SKU Code","Supplier","Storage Type","Cost"]],
        left_on="Menu_Item_Name", right_on="Menu Item Name", how="left"
    )

    no_recipe  = detail[detail["Ingredient"].isna() | (detail["Ingredient"] == "")]
    has_recipe = detail[detail["Ingredient"].notna() & (detail["Ingredient"] != "")].copy()

    # Resolve "Choice of Sauce" rows
    choice_mask = has_recipe["Ingredient"].str.strip().str.lower() == "choice of sauce"
    if choice_mask.any():
        def fix_sauce(row):
            sname = _resolve_sauce_name(row.get("Sauce_Name"))
            if sname and sname in sauce_lookup:
                return pd.Series(sauce_lookup[sname])
            return pd.Series({k: row[k] for k in
                              ["Ingredient","SKU Code","UOM_new","Supplier","Storage Type","Cost"]})
        resolved = has_recipe[choice_mask].apply(fix_sauce, axis=1)
        has_recipe.loc[choice_mask,
            ["Ingredient","SKU Code","UOM_new","Supplier","Storage Type","Cost"]] = resolved.values

    has_recipe["Total_Raw_Qty"] = has_recipe["Quantity"] * has_recipe["Qty_new"]
    has_recipe["Total_Cost"]    = has_recipe["Quantity"] * has_recipe["Cost"]

    # ── Aggregate ─────────────────────────────────────────────────────────────
    # Site-level only (no Brand) — a site may run multiple brands and orders
    # ingredients for all of them together; compliance is always site vs site.
    site_raw = (
        has_recipe
        .groupby(["Store","SKU Code","Ingredient","Supplier","Storage Type","UOM_new"], dropna=False)
        .agg(Total_Raw_Qty=("Total_Raw_Qty","sum"), Total_Cost=("Total_Cost","sum"))
        .reset_index()
        .rename(columns={"SKU Code":"SKU","UOM_new":"UOM"})
        .sort_values(["Store","SKU"])
    )

    raw_summary = (
        has_recipe
        .groupby(["Brand","SKU Code","Ingredient","Supplier","Storage Type","UOM_new"], dropna=False)
        .agg(Total_Raw_Qty=("Total_Raw_Qty","sum"), Total_Cost=("Total_Cost","sum"),
             Sold_Qty=("Quantity","sum"))
        .reset_index()
        .rename(columns={"SKU Code":"SKU","UOM_new":"UOM"})
        .sort_values(["Brand","SKU"])
    )

    ingredient_summary = (
        has_recipe
        .groupby(["SKU Code","Ingredient","Supplier","Storage Type","UOM_new"], dropna=False)
        .agg(Total_Raw_Qty=("Total_Raw_Qty","sum"), Total_Cost=("Total_Cost","sum"))
        .reset_index()
        .rename(columns={"SKU Code":"SKU","UOM_new":"UOM"})
        .sort_values("SKU")
    )

    unmatched_report = pd.concat([
        unmatched_sales[["Brand","Store","Recipe_Item","Item_id","Option_id_1","Quantity","Match_Method"]],
        no_recipe[["Brand","Store","Recipe_Item","Item_id","Option_id_1","Quantity","Menu_Item_Name"]].assign(
            Match_Method="no_recipe_in_builder"
        ).rename(columns={"Menu_Item_Name":"Resolved_Name"})
    ], ignore_index=True).fillna("")

    return site_raw, raw_summary, ingredient_summary, unmatched_report
