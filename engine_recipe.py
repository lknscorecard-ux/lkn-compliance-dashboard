"""
engine_recipe.py — System B Part 1
Maps Items/Options sales to Recipe Items via PLU Mapping rules.
"""

import warnings
import pandas as pd
warnings.filterwarnings("ignore")

IN_SCOPE_BRANDS = {"Hot Chick", "WTF", "Korea Town", "Wing Fest", "Kuro Smash", "Twisted London"}
LKP_META = ["Category", "Recipe_Item"]


def _plu_subset(plu, rule, rename=None):
    df = plu[plu["Rule_Type"] == rule].copy()
    if rename:
        df = df.rename(columns=rename)
    return df


def run(items_df: pd.DataFrame, options_df: pd.DataFrame, plu_df: pd.DataFrame,
        in_scope_brands=None) -> tuple:
    """
    Match item and option sales rows to recipe names via PLU Mapping.

    Returns
    -------
    matched_df         : all matched rows
    summary_df         : grouped by Brand+Recipe_Item+Rule_Type
    unmatched_items_df : item rows with no PLU match
    unmatched_opts_df  : option rows unused as selectors or components
    """
    if in_scope_brands is None:
        in_scope_brands = IN_SCOPE_BRANDS

    # ── Prepare inputs ────────────────────────────────────────────────────────
    plu = plu_df.fillna("").apply(lambda c: c.str.strip() if c.dtype == object else c)

    items = items_df[items_df["Brand"].isin(in_scope_brands)].copy()
    items.columns = items.columns.str.strip()
    items["Quantity"] = pd.to_numeric(items["Quantity"], errors="coerce").fillna(0)
    for c in ["Item ref ID","unique id","Brand","Store","Order ID"]:
        if c in items.columns:
            items[c] = items[c].astype(str).str.strip()

    opts = options_df[options_df["Brand"].isin(in_scope_brands)].copy()
    opts.columns = opts.columns.str.strip()
    opts["Option Quantity"] = pd.to_numeric(opts["Option Quantity"], errors="coerce").fillna(0)
    for c in ["Option Ref ID","unique id","Brand","Store Name","Order ID"]:
        if c in opts.columns:
            opts[c] = opts[c].astype(str).str.strip()
    opts["parent_uid"]   = opts["unique id"].str.rsplit("-", n=1).str[0]
    opts["_opt_row_id"]  = range(len(opts))

    # ── Lookup tables ─────────────────────────────────────────────────────────
    item_only_lkp  = _plu_subset(plu, "ITEM_ONLY",  {"Item_id":"Item ref ID"})[["Item ref ID"]+LKP_META]
    meal_deal_lkp  = _plu_subset(plu, "MEAL_DEAL",  {"Item_id":"Item ref ID"})[["Item ref ID"]+LKP_META]
    combo_lkp      = _plu_subset(plu, "ITEM_OPTION_COMBO",
                                  {"Item_id":"Item ref ID","Option_id_1":"opt_ref_1"}
                                  )[["Item ref ID","opt_ref_1"]+LKP_META]
    multi_lkp_raw  = _plu_subset(plu, "ITEM_MULTI_OPTION_COMBO",
                                  {"Item_id":"Item ref ID","Option_id_1":"opt1","Option_id_2":"opt2"}
                                  )[["Item ref ID","opt1","opt2"]+LKP_META]
    multi_lkp = pd.concat([
        multi_lkp_raw.rename(columns={"opt1":"opt_ref_1","opt2":"opt_ref_2"}),
        multi_lkp_raw.rename(columns={"opt2":"opt_ref_1","opt1":"opt_ref_2"}),
    ], ignore_index=True).drop_duplicates(subset=["Item ref ID","opt_ref_1","opt_ref_2"])

    component_lkp = _plu_subset(plu, "OPTION_COMPONENT", {"Option_id_1":"Option Ref ID"}
                                 )[["Option Ref ID","Category","Recipe_Item"]]
    addon_lkp     = _plu_subset(plu, "STANDALONE_ADDON", {"Option_id_1":"Option Ref ID"}
                                 )[["Option Ref ID","Category","Recipe_Item"]]

    opts_slim = opts[["parent_uid","unique id","Option Ref ID","_opt_row_id"]].rename(
        columns={"unique id":"opt_uid"})

    STORE_COL = "Store" if "Store" in items.columns else "Store Name" if "Store Name" in items.columns else "Order ID"

    # ── Emit helper ───────────────────────────────────────────────────────────
    result_parts      = []
    matched_item_uids = set()
    selector_opt_rows = set()

    def emit(df, rule, item_id_col, opt1_col, opt2_col, qty_col, qty_src):
        out = pd.DataFrame()
        out["Brand"]       = df["Brand"] if "Brand" in df.columns else ""
        out["Store"]       = df[STORE_COL] if STORE_COL in df.columns else ""
        out["Order_ID"]    = df["Order ID"] if "Order ID" in df.columns else ""
        out["Unique_ID"]   = df[item_id_col]
        out["Category"]    = df["Category"]
        out["Recipe_Item"] = df["Recipe_Item"]
        out["Rule_Type"]   = rule
        out["Item_id"]     = df["Item ref ID"] if "Item ref ID" in df.columns else ""
        out["Option_id_1"] = df[opt1_col] if opt1_col and opt1_col in df.columns else ""
        out["Option_id_2"] = df[opt2_col] if opt2_col and opt2_col in df.columns else ""
        out["Quantity"]    = df[qty_col]
        out["Qty_Source"]  = qty_src
        result_parts.append(out)

    # ── A. ITEM_MULTI_OPTION_COMBO ────────────────────────────────────────────
    multi_ids = set(multi_lkp["Item ref ID"])
    cand = items[items["Item ref ID"].isin(multi_ids) & ~items["unique id"].isin(matched_item_uids)]
    step1 = cand.merge(opts_slim.rename(columns={"Option Ref ID":"opt_ref_1","opt_uid":"opt_uid_1","_opt_row_id":"opt_row_id_1"}),
                       left_on="unique id", right_on="parent_uid", how="inner")
    step2 = step1.merge(opts_slim.rename(columns={"Option Ref ID":"opt_ref_2","opt_uid":"opt_uid_2","_opt_row_id":"opt_row_id_2"}),
                        on="parent_uid", how="inner")
    step2 = step2[step2["opt_ref_1"] != step2["opt_ref_2"]]
    multi_hit = step2.merge(multi_lkp, on=["Item ref ID","opt_ref_1","opt_ref_2"], how="inner"
                            ).drop_duplicates(subset=["unique id"])
    if len(multi_hit):
        matched_item_uids.update(multi_hit["unique id"])
        selector_opt_rows.update(multi_hit["opt_row_id_1"])
        selector_opt_rows.update(multi_hit["opt_row_id_2"])
        emit(multi_hit, "ITEM_MULTI_OPTION_COMBO", "unique id", "opt_ref_1", "opt_ref_2", "Quantity", "item_qty")

    # ── B. ITEM_OPTION_COMBO ──────────────────────────────────────────────────
    combo_ids = set(combo_lkp["Item ref ID"])
    cand = items[items["Item ref ID"].isin(combo_ids) & ~items["unique id"].isin(matched_item_uids)]
    cs = cand.merge(opts_slim.rename(columns={"Option Ref ID":"opt_ref_1","opt_uid":"opt_uid_1","_opt_row_id":"opt_row_id_1"}),
                    left_on="unique id", right_on="parent_uid", how="inner")
    combo_hit = cs.merge(combo_lkp, on=["Item ref ID","opt_ref_1"], how="inner"
                         ).drop_duplicates(subset=["unique id"])
    if len(combo_hit):
        matched_item_uids.update(combo_hit["unique id"])
        selector_opt_rows.update(combo_hit["opt_row_id_1"])
        emit(combo_hit, "ITEM_OPTION_COMBO", "unique id", "opt_ref_1", "", "Quantity", "item_qty")

    # ── C. ITEM_ONLY ──────────────────────────────────────────────────────────
    cand = items[items["Item ref ID"].isin(set(item_only_lkp["Item ref ID"])) &
                 ~items["unique id"].isin(matched_item_uids)]
    io_hit = cand.merge(item_only_lkp, on="Item ref ID", how="inner").drop_duplicates(subset=["unique id"])
    if len(io_hit):
        matched_item_uids.update(io_hit["unique id"])
        emit(io_hit, "ITEM_ONLY", "unique id", "", "", "Quantity", "item_qty")

    # ── D. MEAL_DEAL ──────────────────────────────────────────────────────────
    cand = items[items["Item ref ID"].isin(set(meal_deal_lkp["Item ref ID"])) &
                 ~items["unique id"].isin(matched_item_uids)]
    md_hit = cand.merge(meal_deal_lkp, on="Item ref ID", how="inner").drop_duplicates(subset=["unique id"])
    meal_deal_uids = set()
    if len(md_hit):
        matched_item_uids.update(md_hit["unique id"])
        meal_deal_uids = set(md_hit["unique id"])
        emit(md_hit, "MEAL_DEAL", "unique id", "", "", "Quantity", "item_qty")

    unmatched_items_df = items[~items["unique id"].isin(matched_item_uids)].copy()

    # ── Option rows ───────────────────────────────────────────────────────────
    opts_remaining = opts[~opts["_opt_row_id"].isin(selector_opt_rows)].copy()
    used_opt_uids  = set()

    # OPTION_COMPONENT (parent is MEAL_DEAL)
    comp_cand = opts_remaining[opts_remaining["parent_uid"].isin(meal_deal_uids)]
    comp_hit  = comp_cand.merge(component_lkp, on="Option Ref ID", how="inner"
                                ).drop_duplicates(subset=["unique id"])
    if len(comp_hit):
        used_opt_uids.update(comp_hit["_opt_row_id"])
        out = pd.DataFrame({
            "Brand": comp_hit["Brand"], "Store": comp_hit["Store Name"],
            "Order_ID": comp_hit["Order ID"], "Unique_ID": comp_hit["unique id"],
            "Category": comp_hit["Category"], "Recipe_Item": comp_hit["Recipe_Item"],
            "Rule_Type": "OPTION_COMPONENT", "Item_id": "",
            "Option_id_1": comp_hit["Option Ref ID"], "Option_id_2": "",
            "Quantity": comp_hit["Option Quantity"], "Qty_Source": "option_qty",
        })
        result_parts.append(out)

    # STANDALONE_ADDON
    addon_cand = opts_remaining[~opts_remaining["_opt_row_id"].isin(used_opt_uids)]
    addon_hit  = addon_cand.merge(addon_lkp, on="Option Ref ID", how="inner"
                                  ).drop_duplicates(subset=["unique id"])
    if len(addon_hit):
        used_opt_uids.update(addon_hit["_opt_row_id"])
        out = pd.DataFrame({
            "Brand": addon_hit["Brand"], "Store": addon_hit["Store Name"],
            "Order_ID": addon_hit["Order ID"], "Unique_ID": addon_hit["unique id"],
            "Category": addon_hit["Category"], "Recipe_Item": addon_hit["Recipe_Item"],
            "Rule_Type": "STANDALONE_ADDON", "Item_id": "",
            "Option_id_1": addon_hit["Option Ref ID"], "Option_id_2": "",
            "Quantity": addon_hit["Option Quantity"], "Qty_Source": "option_qty",
        })
        result_parts.append(out)

    # Orphaned options (no parent in items)
    all_item_uids  = set(items["unique id"])
    still_rem      = opts_remaining[~opts_remaining["_opt_row_id"].isin(used_opt_uids)].copy()
    orphaned       = still_rem[~still_rem["parent_uid"].isin(all_item_uids)].copy()
    orphaned_used  = set()

    # Orphaned OPTION_COMPONENT
    orp_comp = orphaned.merge(component_lkp, on="Option Ref ID", how="inner"
                              ).drop_duplicates(subset=["unique id"])
    if len(orp_comp):
        orphaned_used.update(orp_comp["_opt_row_id"])
        out = pd.DataFrame({
            "Brand": orp_comp["Brand"], "Store": orp_comp["Store Name"],
            "Order_ID": orp_comp["Order ID"], "Unique_ID": orp_comp["unique id"],
            "Category": orp_comp["Category"], "Recipe_Item": orp_comp["Recipe_Item"],
            "Rule_Type": "OPTION_COMPONENT", "Item_id": "",
            "Option_id_1": orp_comp["Option Ref ID"], "Option_id_2": "",
            "Quantity": orp_comp["Option Quantity"], "Qty_Source": "option_qty",
        })
        result_parts.append(out)

    # Orphaned ITEM_MULTI
    orp_rem = orphaned[~orphaned["_opt_row_id"].isin(orphaned_used)].copy()
    orp_pairs = orp_rem.merge(
        orp_rem[["parent_uid","Option Ref ID","_opt_row_id"]].rename(
            columns={"Option Ref ID":"opt_ref_2","_opt_row_id":"opt_row_id_2"}),
        on="parent_uid", how="inner"
    )
    orp_pairs = orp_pairs[orp_pairs["Option Ref ID"] != orp_pairs["opt_ref_2"]]
    orp_multi_hit = orp_pairs.merge(
        multi_lkp.rename(columns={"opt_ref_1":"Option Ref ID","opt_ref_2":"opt_ref_2"}),
        on=["Option Ref ID","opt_ref_2"], how="inner"
    ).drop_duplicates(subset=["unique id"]).drop_duplicates(subset=["parent_uid","Recipe_Item"])
    if len(orp_multi_hit):
        orphaned_used.update(orp_multi_hit["_opt_row_id"])
        orphaned_used.update(orp_multi_hit["opt_row_id_2"])
        out = pd.DataFrame({
            "Brand": orp_multi_hit["Brand"], "Store": orp_multi_hit["Store Name"],
            "Order_ID": orp_multi_hit["Order ID"], "Unique_ID": orp_multi_hit["unique id"],
            "Category": orp_multi_hit["Category"], "Recipe_Item": orp_multi_hit["Recipe_Item"],
            "Rule_Type": "ITEM_MULTI_OPTION_COMBO",
            "Item_id": orp_multi_hit["Item ref ID"] if "Item ref ID" in orp_multi_hit.columns else "",
            "Option_id_1": orp_multi_hit["Option Ref ID"],
            "Option_id_2": orp_multi_hit["opt_ref_2"],
            "Quantity": orp_multi_hit["Option Quantity"], "Qty_Source": "option_qty",
        })
        result_parts.append(out)

    used_opt_uids.update(orphaned_used)
    unmatched_opts_df = opts_remaining[~opts_remaining["_opt_row_id"].isin(used_opt_uids)].copy()

    # ── Consolidate ───────────────────────────────────────────────────────────
    if result_parts:
        matched_df = pd.concat(result_parts, ignore_index=True)
    else:
        matched_df = pd.DataFrame(columns=[
            "Brand","Store","Order_ID","Unique_ID","Category","Recipe_Item",
            "Rule_Type","Item_id","Option_id_1","Option_id_2","Quantity","Qty_Source"
        ])
    matched_df["Quantity"] = pd.to_numeric(matched_df["Quantity"], errors="coerce").fillna(0)

    summary_df = (
        matched_df
        .groupby(["Brand","Category","Recipe_Item","Rule_Type"], dropna=False)["Quantity"]
        .sum().reset_index()
        .rename(columns={"Quantity":"Total_Qty"})
        .sort_values(["Brand","Category","Recipe_Item"])
    )

    return matched_df, summary_df, unmatched_items_df, unmatched_opts_df
