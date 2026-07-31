#!/usr/bin/env python3
"""
run_pipeline_cloud.py  —  LKN Compliance Pipeline (Cloud Run Job)
==================================================================
Triggered automatically by Cloud Scheduler (every Monday 8am).
Reads weekly files from Google Drive, runs all 3 engines,
writes results to Google Sheets, archives processed files.

Required environment variables:
  GOOGLE_CREDENTIALS_JSON   — service account JSON (as a string)
  DRIVE_UPLOAD_FOLDER_ID    — Google Drive folder where weekly files land
  RESULTS_SHEET_ID          — Google Sheet to write results into
  MAPPING_SHEET_ID          — Google Sheet with Drop Account Mapping (live)

Optional:
  NOTIFICATION_EMAIL        — email address to send run summary to
"""

import os, sys, io, json, logging, warnings
from datetime import datetime, timezone
warnings.filterwarnings("ignore")

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Google API auth ────────────────────────────────────────────────────────────
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def _get_creds() -> Credentials:
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                          os.path.join(os.path.dirname(__file__), "google_credentials.json"))
    if not os.path.exists(path):
        log.error("No credentials found. Set GOOGLE_CREDENTIALS_JSON or place google_credentials.json here.")
        sys.exit(1)
    return Credentials.from_service_account_file(path, scopes=SCOPES)


# ── Drive helpers ──────────────────────────────────────────────────────────────

def _find_file(drive, folder_id: str, keyword: str) -> dict | None:
    q = (f"'{folder_id}' in parents "
         f"and name contains '{keyword}' "
         f"and trashed=false")
    res = drive.files().list(
        q=q, orderBy="modifiedTime desc", pageSize=1,
        fields="files(id,name,modifiedTime)"
    ).execute()
    files = res.get("files", [])
    return files[0] if files else None


def _download(drive, file_id: str) -> io.BytesIO:
    req = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, req, chunksize=10 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


def _archive(drive, file_id: str, folder_id: str):
    """Move a file to an 'archive' sub-folder (create if absent)."""
    q = (f"'{folder_id}' in parents "
         f"and name='archive' "
         f"and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false")
    res   = drive.files().list(q=q, pageSize=1, fields="files(id)").execute()
    found = res.get("files", [])
    if found:
        archive_id = found[0]["id"]
    else:
        meta       = {"name": "archive",
                      "mimeType": "application/vnd.google-apps.folder",
                      "parents": [folder_id]}
        archive_id = drive.files().create(body=meta, fields="id").execute()["id"]
    drive.files().update(
        fileId=file_id,
        addParents=archive_id,
        removeParents=folder_id,
        fields="id,parents",
    ).execute()


# ── Sheets helpers ─────────────────────────────────────────────────────────────

def _write_tab(gc: gspread.Client, sheet_id: str, tab: str, df: pd.DataFrame):
    sh = gc.open_by_key(sheet_id)
    df  = df.fillna("").astype(str)
    try:
        ws = sh.worksheet(tab)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=tab,
            rows=max(len(df) + 10, 100),
            cols=max(len(df.columns) + 2, 20),
        )
    header = [df.columns.tolist()]
    values = df.values.tolist()
    ws.update(header + values)
    log.info("  ✓ %-35s  %d rows", tab, len(df))


def _append_history(gc: gspread.Client, sheet_id: str, tab: str, df: pd.DataFrame):
    """Append df rows to a history tab (creates tab + header if absent)."""
    sh = gc.open_by_key(sheet_id)
    df = df.fillna("").astype(str)
    try:
        ws = sh.worksheet(tab)
        existing = ws.get_all_values()
        if not existing:
            ws.append_rows([df.columns.tolist()] + df.values.tolist())
        else:
            ws.append_rows(df.values.tolist())
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=tab,
            rows=max(len(df) + 200, 1000),
            cols=max(len(df.columns) + 2, 20),
        )
        ws.append_rows([df.columns.tolist()] + df.values.tolist())
    log.info("  ✓ %-35s  +%d rows (history)", tab, len(df))


def _load_stock_on_hand(gc: gspread.Client, sheet_id: str) -> pd.DataFrame:
    """Load previous week's closing stock. Returns empty DataFrame if not found."""
    try:
        sh  = gc.open_by_key(sheet_id)
        ws  = sh.worksheet("Stock on Hand")
        raw = ws.get_all_values()          # safe — no duplicate-header crash
        if not raw or len(raw) < 2:
            log.info("  Stock on Hand: tab empty or header-only — first run?")
            return pd.DataFrame()
        df = pd.DataFrame(raw[1:], columns=raw[0]).fillna("").astype(str)
        df.columns = df.columns.str.strip()
        df = df.loc[:, ~df.columns.duplicated()]
        for c in df.columns:
            df[c] = df[c].str.strip()
        if "Closing_Stock_g" not in df.columns:
            log.warning("  Stock on Hand: 'Closing_Stock_g' column missing — columns: %s", df.columns.tolist())
            return pd.DataFrame()
        df["Closing_Stock_g"] = pd.to_numeric(df["Closing_Stock_g"], errors="coerce").fillna(0)
        # Normalise SKU: strip whitespace, keep as string (preserve leading zeros)
        if "SKU" in df.columns:
            df["SKU"] = df["SKU"].str.strip()
        if "Site_Key" in df.columns:
            df["Site_Key"] = df["Site_Key"].str.strip()
        log.info("  Stock on Hand: %d site×SKU rows loaded (non-zero: %d)",
                 len(df), int((df["Closing_Stock_g"] > 0).sum()))
        return df
    except gspread.exceptions.WorksheetNotFound:
        log.info("  Stock on Hand: tab not found — first run?")
        return pd.DataFrame()
    except Exception as _e:
        log.warning("  Stock on Hand: failed to load (%s) — proceeding with zero opening stock", _e)
        return pd.DataFrame()


def _append_run_log(gc, sheet_id, run_ts, found, stats):
    sh  = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet("Run Log")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("Run Log", 1000, 12)
        ws.update([["Timestamp", "Bidfood File", "Items File", "Options File",
                    "Sites", "Compliance Rows", "Surplus", "Deficit", "Exact", "Status"]])
    ws.append_row([
        run_ts,
        found["bidfood"]["name"],
        found["items"]["name"],
        found["options"]["name"],
        stats["sites"],
        stats["compliance_rows"],
        stats["surplus"],
        stats["deficit"],
        stats["exact"],
        "OK",
    ])


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info("=" * 60)
    log.info("LKN Compliance Pipeline  —  %s", run_ts)
    log.info("=" * 60)

    # Validate env
    for var in ["DRIVE_UPLOAD_FOLDER_ID", "RESULTS_SHEET_ID", "MAPPING_SHEET_ID"]:
        if not os.environ.get(var):
            log.error("Missing required env var: %s", var)
            sys.exit(1)

    creds     = _get_creds()
    drive     = build("drive", "v3", credentials=creds, cache_discovery=False)
    gc        = gspread.authorize(creds)
    folder_id = os.environ["DRIVE_UPLOAD_FOLDER_ID"]
    results_id= os.environ["RESULTS_SHEET_ID"]
    mapping_id= os.environ["MAPPING_SHEET_ID"]

    # ── [1/6] Find weekly files in Drive ──────────────────────────────────────
    log.info("[1/6] Scanning Google Drive folder for weekly files ...")
    # Required files
    REQUIRED_KEYWORDS = {
        "bidfood": "processed_data",
        "items":   "Items-wise",
        "options": "Options-wise",
    }
    # Optional files
    OPTIONAL_KEYWORDS = {
        "opalion":    "Line Item Details",  # Opalion packaging CSV
        "stock_seed": "stock_seed",         # One-time opening stock seed (previous week's processed_data renamed)
    }
    found = {}
    for key, kw in {**REQUIRED_KEYWORDS, **OPTIONAL_KEYWORDS}.items():
        f = _find_file(drive, folder_id, kw)
        if f:
            log.info("  Found %-10s → %s  (%s)", key, f["name"], f["modifiedTime"][:10])
            found[key] = f
        elif key in REQUIRED_KEYWORDS:
            log.warning("  NOT FOUND: keyword='%s'", kw)
        else:
            log.info("  Optional not found: keyword='%s' (skipping packaging)", kw)

    required_found = [k for k in REQUIRED_KEYWORDS if k in found]
    if len(required_found) < 3:
        log.error("Only %d/3 required files found — aborting run.", len(required_found))
        sys.exit(1)

    # ── [2/6] Download files ──────────────────────────────────────────────────
    log.info("[2/6] Downloading files from Google Drive ...")
    bf_buf   = _download(drive, found["bidfood"]["id"])
    it_buf   = _download(drive, found["items"]["id"])
    op_buf   = _download(drive, found["options"]["id"])

    bf_df      = pd.read_excel(bf_buf,  dtype=str, engine="openpyxl")
    items_df   = pd.read_excel(it_buf,  dtype=str, engine="openpyxl")
    options_df = pd.read_excel(op_buf,  dtype=str, engine="openpyxl")
    for df in [bf_df, items_df, options_df]:
        df.columns = df.columns.str.strip()
    log.info("  Bidfood %d rows | Items %d rows | Options %d rows",
             len(bf_df), len(items_df), len(options_df))

    # Opalion is optional — load if present
    opalion_df = None
    if "opalion" in found:
        opal_buf   = _download(drive, found["opalion"]["id"])
        opalion_df = pd.read_csv(opal_buf)
        opalion_df.columns = opalion_df.columns.str.strip()
        log.info("  Opalion  %d rows", len(opalion_df))

    # ── [3/6] Load Drop Account Mapping (live Google Sheet) ───────────────────
    log.info("[3/6] Loading Drop Account Mapping from Google Sheets ...")
    mapping_ws = gc.open_by_key(mapping_id).worksheet("Site Mapping")
    _raw = mapping_ws.get_all_values()
    mapping_df = (
        pd.DataFrame(_raw[1:], columns=_raw[0]).fillna("").astype(str)
        if _raw else pd.DataFrame()
    )
    mapping_df.columns = mapping_df.columns.str.strip()              # strip header whitespace
    mapping_df = mapping_df.loc[:, ~mapping_df.columns.duplicated()]  # drop duplicate headers
    for col in mapping_df.select_dtypes("object").columns:
        mapping_df[col] = mapping_df[col].str.strip()
    # Filter to Required = YES sites only
    if "Required" in mapping_df.columns:
        _before = len(mapping_df)
        mapping_df = mapping_df[mapping_df["Required"].str.upper() == "YES"].copy()
        log.info("  Mapping: %d sites loaded (%d excluded — Required≠YES)", len(mapping_df), _before - len(mapping_df))
    else:
        log.info("  Mapping: %d sites loaded", len(mapping_df))

    # ── [3b] Seed "Stock on Hand" from previous week's Bidfood file (one-time) ──
    # Upload last week's processed_data renamed to include "stock_seed" in the
    # filename alongside the 3 weekly files. The pipeline converts it to grams,
    # writes "Stock on Hand", archives the file, and never needs it again.
    if "stock_seed" in found and _load_stock_on_hand(gc, results_id).empty:
        log.info("[3b] Stock seed file found — seeding opening stock ...")
        try:
            seed_buf = _download(drive, found["stock_seed"]["id"])
            seed_df  = pd.read_excel(seed_buf, dtype=str, engine="openpyxl")
            seed_df.columns = seed_df.columns.str.strip()
            # Reuse engine_bidfood to convert cases → grams per site×SKU
            import engine_bidfood as _eb
            _seed_stock, _, _, _ = _eb.run(seed_df, mapping_df)
            _seed_soh = (
                _seed_stock
                .groupby(["Site_Key", "Product Code"], as_index=False)
                .agg(Closing_Stock_g=("Total_Ordered_Qty", "sum"))
                .rename(columns={"Product Code": "SKU"})
            )
            _seed_soh["Closing_Stock_g"] = pd.to_numeric(
                _seed_soh["Closing_Stock_g"], errors="coerce"
            ).fillna(0).round(1)
            _seed_soh["Week_Commencing"] = "seed"
            _write_tab(gc, results_id, "Stock on Hand", _seed_soh)
            _archive(drive, found["stock_seed"]["id"], folder_id)
            log.info("  Stock seed: %d site×SKU rows written to 'Stock on Hand'", len(_seed_soh))
        except Exception as _se:
            log.warning("  Stock seed failed: %s — continuing with zero opening stock", _se)
    elif "stock_seed" in found:
        log.info("  Stock seed file found but 'Stock on Hand' already exists — skipping seed, archiving.")
        _archive(drive, found["stock_seed"]["id"], folder_id)

    # ── [4/6] Load static reference files (baked into Docker image) ───────────
    log.info("[4/6] Loading static reference files ...")
    APP_DIR  = os.path.dirname(os.path.abspath(__file__))
    PLU_FILE = os.path.join(APP_DIR, "PLU_Mapping_Complete.xlsx")
    RB_FILE  = os.path.join(APP_DIR, "Recipe builder.xlsx")

    plu_df = (pd.read_excel(PLU_FILE, sheet_name="Master PLU Mapping",
                             dtype=str, engine="calamine")
              .fillna("")
              .apply(lambda c: c.str.strip() if c.dtype == object else c))

    # ── [5/6] Run engines ─────────────────────────────────────────────────────
    import engine_recipe, engine_ingredient, engine_bidfood, engine_opalion, engine_compliance

    log.info("[5/6] Running System B — recipe matching ...")
    matched_df, summary_df, unmatched_items, unmatched_opts = engine_recipe.run(
        items_df, options_df, plu_df
    )
    log.info("  Matched: %d rows", len(matched_df))

    log.info("         System B — ingredient requirements ...")
    site_raw, raw_summary, ingredient_summary, unmatched_report = engine_ingredient.run(
        matched_df, RB_FILE
    )
    log.info("  Site raw material: %d rows", len(site_raw))

    log.info("         System A — Bidfood stock ...")
    site_stock, sku_summary, bf_lkn, bf_unmatched = engine_bidfood.run(bf_df, mapping_df)
    log.info("  Site stock: %d rows", len(site_stock))

    # ── Filter to LKN food SKUs only ─────────────────────────────────────────
    _LKN_FOOD_SKUS = {
        "34188","30110","6583","06583","15661","18363",
        "25788","26214","22667","22668","26222","26227","26229","29053","30003",
        "26364","17339",
    }
    site_raw   = site_raw[site_raw["SKU"].astype(str).isin(_LKN_FOOD_SKUS)].copy()
    site_stock = site_stock[site_stock["Product Code"].astype(str).isin(_LKN_FOOD_SKUS)].copy()
    log.info("  After SKU filter — site_raw: %d rows | site_stock: %d rows",
             len(site_raw), len(site_stock))

    log.info("         System C — compliance gap ...")
    # Build Store Name → Site Key lookup for req-only rows with no Bidfood match
    _store_site_map = dict(zip(
        mapping_df["Store Name"].str.strip(),
        mapping_df["Site Key"].str.strip(),
    ))
    # Load previous week's closing stock (rolling inventory carry-forward)
    log.info("         Loading Stock on Hand (opening stock carry-forward) ...")
    _opening_stock = _load_stock_on_hand(gc, results_id)
    if not _opening_stock.empty:
        log.info("  Opening stock: %d site×SKU rows loaded", len(_opening_stock))
    else:
        log.info("  Opening stock: none found — first run or reset")

    compliance = engine_compliance.run(
        site_raw, site_stock,
        store_site_map=_store_site_map,
        opening_stock=_opening_stock if not _opening_stock.empty else None,
    )
    site_summ  = engine_compliance.site_summary(compliance)

    surplus = int((compliance["Status"] == "Surplus").sum())
    deficit = int((compliance["Status"] == "Deficit").sum())
    exact   = int((compliance["Status"] == "Exact").sum())
    log.info("  Surplus %d | Deficit %d | Exact %d", surplus, deficit, exact)

    # ── Per-SKU compliance tolerance (portions) ───────────────────────────────
    # If Gap >= -(tolerance_portions × Qty_new_g), treat as Compliant even if
    # slightly short. Carry-forward stock is already included in Gap.
    _TOLERANCE_PORTIONS = {
        "34188": 1, "30110": 1, "6583":  1, "06583": 1,
        "15661": 1, "18363": 1, "25788": 1,
        "26364": 1, "17339": 1,
        "26214": 2,
        "22667": 2, "22668": 2, "26222": 2, "26227": 2,
        "26229": 2, "29053": 2, "30003": 2,
    }

    # ── Portion size enrichment ───────────────────────────────────────────────
    # Divide Required_Qty / Ordered_Qty / Gap by Qty_new (g per portion) from
    # Recipe Builder, matched by SKU, to express compliance in portions.
    try:
        rb_xl2       = pd.ExcelFile(RB_FILE, engine="calamine")
        _rb_ps_parts = []
        for _sht in ["Hot Chick", "WTF", "Koreatown", "Wing Fest", "Kurosmash"]:
            try:
                _d = rb_xl2.parse(_sht, dtype=str)
                _d.columns = _d.columns.str.strip()
                _rb_ps_parts.append(_d)
            except Exception:
                pass
        if _rb_ps_parts:
            _rb_ps = pd.concat(_rb_ps_parts, ignore_index=True)
            _rb_ps["SKU Code"] = _rb_ps["SKU Code"].fillna("").str.strip()
            _rb_ps["Qty_new"]  = pd.to_numeric(_rb_ps["Qty_new"], errors="coerce")
            _sku_qty = (
                _rb_ps[_rb_ps["SKU Code"] != ""]
                .groupby("SKU Code")["Qty_new"]
                .mean()
            )
            _qty_per = pd.to_numeric(
                compliance["SKU"].astype(str).map(_sku_qty), errors="coerce"
            )
            _valid = _qty_per.notna() & (_qty_per > 0)
            # Use .where() so non-mapped rows are NaN (fillna("") in _write_tab handles them)
            compliance["Portion_Required"]       = (compliance["Required_Qty"]   / _qty_per).round(1).where(_valid)
            compliance["Portion_Ordered"]        = (compliance["Ordered_Qty"]    / _qty_per).round(1).where(_valid)
            compliance["Portion_Gap"]            = (compliance["Gap"]            / _qty_per).round(1).where(_valid)
            compliance["Carry_Forward_Portions"] = (compliance["Closing_Stock_g"] / _qty_per).round(1).where(_valid)
            log.info("  Portion size: %.0f%% of rows mapped", _valid.mean() * 100)

            # ── Apply per-SKU tolerance ───────────────────────────────────────
            # Tolerance in grams = tolerance_portions × Qty_new_g per SKU
            _tol_por = (
                compliance["SKU"].astype(str).str.strip()
                .map(_TOLERANCE_PORTIONS).fillna(0)
            )
            _tol_g = (_tol_por * _qty_per.fillna(0)).fillna(0)
            _gap   = compliance["Gap"]
            compliance["Status"] = [
                "Surplus" if g > 0
                else ("Exact"   if g >= -t   # within tolerance → treat as compliant
                else  "Deficit")
                for g, t in zip(_gap, _tol_g)
            ]
            n_rescued = int((_tol_g > 0).sum() and
                            (compliance["Status"] == "Exact").sum())
            log.info("  Tolerance applied — %d rows re-evaluated", n_rescued)
    except Exception as _e:
        log.warning("  Portion size enrichment skipped: %s", _e)

    # ── Case enrichment ───────────────────────────────────────────────────────
    # Cases = Qty_g / Pack_Qty_g  (Pack_Qty from Bidfood site_stock, g per case)
    try:
        if "Pack_Qty" in site_stock.columns:
            _pk_map = (
                site_stock.dropna(subset=["Pack_Qty"])
                .groupby(["Site_Key", "Product Code"])["Pack_Qty"]
                .first()
            )
            _keys  = list(zip(
                compliance["Site_Key"].astype(str).str.strip(),
                compliance["SKU"].astype(str).str.strip(),
            ))
            _pk = pd.to_numeric(
                pd.Series([_pk_map.get(k, float("nan")) for k in _keys], index=compliance.index),
                errors="coerce",
            )
            _valid_pk = _pk.notna() & (_pk > 0)
            compliance["Cases_Required"] = (compliance["Required_Qty"] / _pk).round(2).where(_valid_pk)
            compliance["Cases_Ordered"]  = (compliance["Ordered_Qty"]  / _pk).round(2).where(_valid_pk)
            compliance["Cases_Gap"]      = (compliance["Gap"]          / _pk).round(2).where(_valid_pk)
            log.info("  Case enrichment: %.0f%% of rows mapped", _valid_pk.mean() * 100)
    except Exception as _e:
        log.warning("  Case enrichment skipped: %s", _e)

    # ── Week commencing tag ───────────────────────────────────────────────────
    from datetime import timedelta
    _run_dt = datetime.now(timezone.utc)
    _wc = (_run_dt - timedelta(days=_run_dt.weekday() + 7)).strftime("%Y-%m-%d")  # previous Monday
    compliance.insert(0, "Week_Commencing", _wc)
    site_summ.insert(0, "Week_Commencing", _wc)

    # ── Save closing stock → next week's opening stock ────────────────────────
    # One row per Site × SKU with the stock remaining after this week's consumption.
    _stock_cols = [c for c in ["Site_Key","SKU","Ingredient","Closing_Stock_g","Week_Commencing"]
                   if c in compliance.columns]
    _stock_on_hand = (
        compliance[_stock_cols]
        .groupby(["Site_Key","SKU"], as_index=False)
        .agg(
            Ingredient      =("Ingredient",      "first"),
            Closing_Stock_g =("Closing_Stock_g", "sum"),
            Week_Commencing =("Week_Commencing",  "first"),
        )
    )
    _write_tab(gc, results_id, "Stock on Hand", _stock_on_hand)

    # ── Append this week to history sheets (never overwritten) ────────────────
    _append_history(gc, results_id, "Compliance History",    compliance)
    _append_history(gc, results_id, "Site Summary History",  site_summ)

    # Packaging (Opalion) — disabled; calculated separately
    pkg_compliance  = pd.DataFrame()
    pkg_site_summ   = pd.DataFrame()
    pkg_sku_summary = pd.DataFrame()

    # ── [6/6] Write results to Google Sheets ──────────────────────────────────
    log.info("[6/6] Writing results to Google Sheets ...")

    # Tag site_stock with week commencing for historical spend tracking
    site_stock_wc = site_stock.copy()
    site_stock_wc.insert(0, "Week_Commencing", _wc)

    _write_tab(gc, results_id, "Compliance Gap",           compliance)
    _write_tab(gc, results_id, "Site Summary",             site_summ)
    _write_tab(gc, results_id, "Ingredient Requirements",  site_raw)
    _write_tab(gc, results_id, "Bidfood Stock",            site_stock_wc)
    _append_history(gc, results_id, "Bidfood Stock History", site_stock_wc)
    _write_tab(gc, results_id, "SKU Summary",              ingredient_summary)
    _write_tab(gc, results_id, "Recipe Summary",           summary_df)
    _write_tab(gc, results_id, "Unmatched",                unmatched_report)
    if not bf_unmatched.empty:
        bf_unmatched_wc = bf_unmatched.copy()
        bf_unmatched_wc.insert(0, "Week_Commencing", _wc)
        _write_tab(gc, results_id, "Bidfood Unmatched", bf_unmatched_wc)
        _append_history(gc, results_id, "Bidfood Unmatched History", bf_unmatched_wc)
    if not pkg_compliance.empty:
        _write_tab(gc, results_id, "Packaging Compliance",  pkg_compliance)
        _write_tab(gc, results_id, "Packaging Site Summary", pkg_site_summ)
        _write_tab(gc, results_id, "Packaging Products",     pkg_sku_summary)

    stats = {
        "sites":           site_summ.shape[0],
        "compliance_rows": len(compliance),
        "surplus":         surplus,
        "deficit":         deficit,
        "exact":           exact,
    }
    _append_run_log(gc, results_id, run_ts, found, stats)

    # Archive processed files (all found files including optional Opalion)
    log.info("Archiving processed files ...")
    for key, f in found.items():
        _archive(drive, f["id"], folder_id)
        log.info("  Archived: %s", f["name"])

    log.info("=" * 60)
    log.info("Pipeline complete — %s", run_ts)
    log.info("  Sites monitored : %d", stats["sites"])
    log.info("  Compliance rows : %d  (Surplus %d | Deficit %d | Exact %d)",
             stats["compliance_rows"], surplus, deficit, exact)
    log.info("  Results written to sheet: %s", results_id)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
