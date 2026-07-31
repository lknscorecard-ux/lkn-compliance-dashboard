"""
dashboard_cloud.py  —  LKN Compliance Dashboard (Cloud Version)
================================================================
Permanently hosted on Streamlit Community Cloud.
Reads live from Google Sheets — no file uploads, no local Python needed.

Deploy: connect your GitHub repo to https://share.streamlit.io
Secrets required: gcp_service_account (JSON object) + RESULTS_SHEET_ID
"""

import os, warnings
warnings.filterwarnings("ignore")

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from google.oauth2.service_account import Credentials
import gspread

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LKN Compliance Dashboard",
    page_icon="🍗",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  [data-testid="stMetricValue"] { font-size: 2rem; font-weight: 700; }
  [data-testid="stMetricLabel"] { font-size: 0.8rem; color: #555; }
  .stDataFrame { border-radius: 8px; }
  div[data-testid="stHorizontalBlock"] > div { gap: 0.5rem; }
</style>
""", unsafe_allow_html=True)

# ── Constants ──────────────────────────────────────────────────────────────────
RESULTS_SHEET_ID = os.environ.get("RESULTS_SHEET_ID", "")
MAPPING_SHEET_ID = "1P94V4FDrx9TFmDhUuPWIP2EGz4Dp2gCmjJfTkoebTUw"
PROJECT_ID = "compliance-501910"
REGION     = "europe-west2"
JOB_NAME   = "lkn-pipeline"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/cloud-platform",
]

# ── Auth ───────────────────────────────────────────────────────────────────────
@st.cache_resource(ttl=3600)
def _get_creds():
    info = dict(st.secrets["gcp_service_account"])
    return Credentials.from_service_account_info(info, scopes=SCOPES)

def _get_gc():
    return gspread.authorize(_get_creds())

def _trigger_pipeline():
    import google.auth.transport.requests, requests as _req
    creds = _get_creds()
    creds.refresh(google.auth.transport.requests.Request())
    url  = (f"https://{REGION}-run.googleapis.com/apis/run.googleapis.com/v1"
            f"/namespaces/{PROJECT_ID}/jobs/{JOB_NAME}:run")
    resp = _req.post(url, headers={"Authorization": f"Bearer {creds.token}"})
    if not resp.ok:
        raise RuntimeError(resp.text)

# ── Data loading ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def _load_all_sheets() -> dict:
    import time
    out = {}
    sid = (st.secrets.get("RESULTS_SHEET_ID")
           or os.environ.get("RESULTS_SHEET_ID", RESULTS_SHEET_ID))
    if not sid:
        out["_load_errors"] = ["RESULTS_SHEET_ID secret is not set."]
        return out

    # Retry up to 3 times with backoff — handles transient quota / 503 errors
    sh = None
    for _attempt in range(3):
        try:
            gc = _get_gc()
            sh = gc.open_by_key(sid)
            break
        except gspread.exceptions.APIError as _e:
            if _attempt < 2:
                time.sleep(4 ** _attempt)   # 1s, 4s
            else:
                out["_load_errors"] = [f"Google Sheets API error after 3 attempts: {_e}"]
                return out
        except Exception as _e:
            out["_load_errors"] = [f"Failed to open Google Sheet: {_e}"]
            return out

    def _read_tab(ws_name, tail_rows=None):
        """Read a worksheet safely. tail_rows=N reads only the last N data rows (+ header)."""
        try:
            ws  = sh.worksheet(ws_name)
            if tail_rows:
                # Read header + last N rows only — avoids loading huge history sheets
                all_vals = ws.get_all_values()
                if not all_vals or len(all_vals) < 2:
                    return pd.DataFrame()
                header = all_vals[0]
                # Strip trailing blank rows before slicing
                data = [r for r in all_vals[1:] if any(c.strip() for c in r)]
                raw  = [header] + data[-tail_rows:]
            else:
                raw = ws.get_all_values()
            if not raw or len(raw) < 2:
                return pd.DataFrame()
            # Strip trailing blank rows
            data_rows = [r for r in raw[1:] if any(c.strip() for c in r)]
            if not data_rows:
                return pd.DataFrame()
            df = pd.DataFrame(data_rows, columns=raw[0]).fillna("").astype(str)
            df.columns = df.columns.str.strip()
            df = df.loc[:, ~df.columns.duplicated()]
            return df
        except gspread.exceptions.WorksheetNotFound:
            return pd.DataFrame()   # tab not yet created — silent
        except Exception as _e:
            out.setdefault("_load_errors", []).append(f"{ws_name}: {_e}")
            return pd.DataFrame()

    # History tabs: read last 3 weeks of rows only to avoid timeout on large sheets.
    # Each week ≈ 100-150 sites × 16 SKUs ≈ 1,600 rows → 3 weeks = 5,000 rows max.
    HISTORY_TAIL = 5000

    def _has_valid_site_keys(df):
        """Return True if at least 50% of Site_Key rows are non-blank."""
        if "Site_Key" not in df.columns or df.empty:
            return True  # no Site_Key column — don't reject
        n_valid = (df["Site_Key"].astype(str).str.strip() != "").sum()
        return n_valid >= max(1, len(df) * 0.5)

    # Compliance Gap: combine current + history so all weeks appear in the W/C filter.
    # Current tab has the latest week; History tab has prior weeks.
    # Deduplicate on (Site_Key, SKU, Week_Commencing) keeping the current-tab row.
    _comp_cur  = _read_tab("Compliance Gap")
    time.sleep(1)
    _comp_hist = _read_tab("Compliance History", tail_rows=HISTORY_TAIL)
    time.sleep(1)
    if _comp_cur.empty and _comp_hist.empty:
        out["Compliance Gap"] = pd.DataFrame()
    elif _comp_cur.empty:
        out["Compliance Gap"] = _comp_hist if _has_valid_site_keys(_comp_hist) else pd.DataFrame()
    elif _comp_hist.empty or not _has_valid_site_keys(_comp_hist):
        out["Compliance Gap"] = _comp_cur
    else:
        # Keep history rows whose Week_Commencing is NOT in the current tab
        _cur_wks  = set(_comp_cur.get("Week_Commencing", pd.Series()).dropna().unique())
        _hist_old = (
            _comp_hist[~_comp_hist.get("Week_Commencing", pd.Series(dtype=str))
                       .isin(_cur_wks)]
            if "Week_Commencing" in _comp_hist.columns
            else pd.DataFrame()
        )
        _hist_old = _hist_old[_has_valid_site_keys(_hist_old)] if not _hist_old.empty else _hist_old
        out["Compliance Gap"] = (
            pd.concat([_comp_cur, _hist_old], ignore_index=True)
            if not _hist_old.empty else _comp_cur
        )

    # Site Summary: same combine logic
    _ss_cur  = _read_tab("Site Summary")
    time.sleep(1)
    _ss_hist = _read_tab("Site Summary History", tail_rows=HISTORY_TAIL)
    time.sleep(1)
    if _ss_cur.empty:
        out["Site Summary"] = _ss_hist
    elif _ss_hist.empty:
        out["Site Summary"] = _ss_cur
    else:
        _cur_ss_wks  = set(_ss_cur.get("Week_Commencing", pd.Series()).dropna().unique())
        _hist_ss_old = (
            _ss_hist[~_ss_hist.get("Week_Commencing", pd.Series(dtype=str)).isin(_cur_ss_wks)]
            if "Week_Commencing" in _ss_hist.columns
            else pd.DataFrame()
        )
        out["Site Summary"] = (
            pd.concat([_ss_cur, _hist_ss_old], ignore_index=True)
            if not _hist_ss_old.empty else _ss_cur
        )

    for tab in ["Ingredient Requirements", "Run Log"]:
        out[tab] = _read_tab(tab)
        time.sleep(1)
    # Bidfood Stock: prefer history (last 3 weeks) over current tab
    _bf_hist = _read_tab("Bidfood Stock History", tail_rows=HISTORY_TAIL)
    out["Bidfood Stock"] = _bf_hist if not _bf_hist.empty else _read_tab("Bidfood Stock")
    time.sleep(1)
    return out

def _safe(tab: str) -> pd.DataFrame:
    return _load_all_sheets().get(tab, pd.DataFrame())

@st.cache_data(ttl=600, show_spinner=False)
def _load_site_mapping() -> pd.DataFrame:
    """
    Load the Site Mapping sheet and return a normalised DataFrame.
    Columns of interest: Site Key, Required, Account Manager.
    Returns empty DataFrame on failure.
    """
    try:
        mid = MAPPING_SHEET_ID
        if not mid:
            return pd.DataFrame()
        gc  = _get_gc()
        ws  = gc.open_by_key(mid).worksheet("Site Mapping")
        raw = ws.get_all_values()
        if not raw or len(raw) < 2:
            return pd.DataFrame()
        df  = pd.DataFrame(raw[1:], columns=raw[0])
        df.columns = df.columns.str.strip()
        df  = df.loc[:, ~df.columns.duplicated()]
        for c in df.select_dtypes("object").columns:
            df[c] = df[c].str.strip()
        # Normalise Site Key column name (might be "Site Key" or "Site_Key")
        if "Site_Key" in df.columns and "Site Key" not in df.columns:
            df = df.rename(columns={"Site_Key": "Site Key"})
        return df
    except Exception:
        return pd.DataFrame()

def _load_required_sites() -> set:
    """Return set of Site_Keys where Required = YES in the mapping sheet."""
    df = _load_site_mapping()
    if df.empty or "Required" not in df.columns or "Site Key" not in df.columns:
        return set()
    return set(df[df["Required"].str.upper() == "YES"]["Site Key"].tolist())

def _to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

# ── Header ─────────────────────────────────────────────────────────────────────
import base64, pathlib
def _logo_b64():
    try:
        p = pathlib.Path(__file__).parent / "lkn_logo.png"
        return base64.b64encode(p.read_bytes()).decode()
    except Exception:
        return ""

_logo = _logo_b64()

h1, h2, h3 = st.columns([3, 1, 1])
with h1:
    if _logo:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:16px;">'
            f'<img src="data:image/png;base64,{_logo}" style="height:70px;width:auto;border:2px solid black;padding:4px;">'
            f'<span style="font-size:1.8rem;font-weight:700;">LKN Compliance Dashboard</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.title("LKN Compliance Dashboard")
with h2:
    if st.button("▶ Run Pipeline Now", type="primary", use_container_width=True):
        with st.spinner("Triggering pipeline …"):
            try:
                _trigger_pipeline()
                st.success("Pipeline started — results update in ~2 min.")
            except Exception as e:
                st.error(f"Failed: {e}")
with h3:
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Load all data ──────────────────────────────────────────────────────────────
with st.spinner("Loading data from Google Sheets …"):
    _all_data    = _load_all_sheets()
    compliance   = _all_data.get("Compliance Gap",          pd.DataFrame())
    site_summ    = _all_data.get("Site Summary",            pd.DataFrame())
    ingredient_s = _all_data.get("Ingredient Requirements", pd.DataFrame())
    bidfood_s    = _all_data.get("Bidfood Stock",           pd.DataFrame())
    run_log      = _all_data.get("Run Log",                 pd.DataFrame())
    # Show any sheet-read errors for debugging
    for _err in _all_data.get("_load_errors", []):
        st.warning(f"⚠️ Sheet load error: {_err}")

# ── Strip blank rows (Google Sheets trailing empty rows) ──────────────────────
if "Site_Key" in compliance.columns:
    compliance = compliance[compliance["Site_Key"].str.strip() != ""]
if "SKU" in compliance.columns:
    compliance = compliance[compliance["SKU"].str.strip() != ""]
if "Site_Key" in site_summ.columns:
    site_summ = site_summ[site_summ["Site_Key"].str.strip() != ""]

# ── Load mapping sheet (required sites + account managers) ────────────────────
with st.spinner("Checking required sites…"):
    _site_mapping   = _load_site_mapping()
    _required_sites = _load_required_sites()

# Build Site_Key → Account Manager lookup
_site_key_to_manager: dict = {}
if not _site_mapping.empty and "Site Key" in _site_mapping.columns and "Account Manager" in _site_mapping.columns:
    _site_key_to_manager = (
        _site_mapping.set_index("Site Key")["Account Manager"]
        .replace("", pd.NA).dropna()
        .to_dict()
    )
if _required_sites:
    _before = len(compliance)
    if "Site_Key" in compliance.columns:
        compliance = compliance[compliance["Site_Key"].isin(_required_sites)]
    if "Site_Key" in site_summ.columns:
        site_summ  = site_summ[site_summ["Site_Key"].isin(_required_sites)]
    # Safety: filter wiped everything (or data never loaded) → show unfiltered
    if compliance.empty:
        compliance = _safe("Compliance Gap")
        if "Site_Key" in compliance.columns:
            compliance = compliance[compliance["Site_Key"].str.strip() != ""]
        if "SKU" in compliance.columns:
            compliance = compliance[compliance["SKU"].str.strip() != ""]
        if not compliance.empty and _before > 0:
            st.warning("⚠️ Required Sites filter removed all data — showing unfiltered. Check Site Key values in mapping sheet.")

if compliance.empty:
    st.error("No compliance data loaded from Google Sheets.")
    with st.expander("🔍 Debug info — expand to diagnose", expanded=True):
        _tab_info = {
            k: (f"{len(v)} rows, cols: {list(v.columns[:5])}" if isinstance(v, pd.DataFrame) and not v.empty
                else ("empty" if isinstance(v, pd.DataFrame) else str(v)))
            for k, v in _all_data.items() if k != "_load_errors"
        }
        st.json(_tab_info)
        if _all_data.get("_load_errors"):
            st.error("Sheet errors: " + " | ".join(_all_data["_load_errors"]))
        st.write(f"Required sites loaded: {len(_required_sites)}")
        # Show sample keys from each side to diagnose mismatch
        _raw_comp = _all_data.get("Compliance Gap", pd.DataFrame())
        if not _raw_comp.empty and "Site_Key" in _raw_comp.columns:
            st.write("Sample Site_Keys in compliance data:",
                     _raw_comp["Site_Key"].dropna().unique()[:8].tolist())
        if _required_sites:
            st.write("Sample required Site_Keys (mapping sheet):",
                     sorted(list(_required_sites))[:8])
    st.info("Click **🔄 Refresh Data** above. If this persists, run the pipeline first.")
    st.stop()

# ── Numeric coercion ───────────────────────────────────────────────────────────
_num_compliance = ["Required_Qty", "Ordered_Qty", "Gap",
                   "Opening_Stock_g", "Closing_Stock_g",
                   "Portion_Required", "Portion_Ordered", "Portion_Gap",
                   "Carry_Forward_Portions"]
for c in _num_compliance:
    if c in compliance.columns:
        compliance[c] = pd.to_numeric(compliance[c], errors="coerce").fillna(0)

_num_site = ["Compliance_%", "Total_SKUs", "Surplus", "Deficit", "Exact"]
for c in _num_site:
    if c in site_summ.columns:
        site_summ[c] = pd.to_numeric(site_summ[c], errors="coerce").fillna(0)

# Recompute Compliance % from Total_SKUs so existing history data is also correct
if all(c in site_summ.columns for c in ["Surplus", "Exact", "Total_SKUs"]):
    _comp = site_summ["Surplus"] + site_summ["Exact"]
    _tot  = site_summ["Total_SKUs"].replace(0, float("nan"))
    site_summ["Compliance_%"] = (_comp / _tot * 100).round(1).fillna(0)

for c in ["Total_Raw_Qty", "Total_Cost"]:
    if c in ingredient_s.columns:
        ingredient_s[c] = pd.to_numeric(ingredient_s[c], errors="coerce").fillna(0)

# ── Derive Carry_Forward_Portions from existing columns ──────────────────────
# Qty_new (g per portion) derived from Required side, fallback to Ordered side.
# This avoids "None" when Required_Qty = 0 (site ordered but has no recipe req).
if "Closing_Stock_g" in compliance.columns:
    _cls_g = pd.to_numeric(compliance["Closing_Stock_g"], errors="coerce").fillna(0)
    _qty_new = pd.Series(float("nan"), index=compliance.index)
    if all(c in compliance.columns for c in ["Required_Qty", "Portion_Required"]):
        _rq = pd.to_numeric(compliance["Required_Qty"], errors="coerce").fillna(0)
        _pr = pd.to_numeric(compliance["Portion_Required"], errors="coerce").fillna(0)
        _qty_new = (_rq / _pr).where(_pr > 0)
    if all(c in compliance.columns for c in ["Ordered_Qty", "Portion_Ordered"]):
        _oq = pd.to_numeric(compliance["Ordered_Qty"], errors="coerce").fillna(0)
        _po = pd.to_numeric(compliance["Portion_Ordered"], errors="coerce").fillna(0)
        _qty_new = _qty_new.fillna((_oq / _po).where(_po > 0))
    compliance["Carry_Forward_Portions"] = (_cls_g / _qty_new).round(1)

# ── Per-SKU compliance tolerance (applied in dashboard as pipeline fallback) ──
# If Portion_Gap >= -tolerance_portions, reclassify Deficit → Exact (Compliant).
# Carry-forward stock is already included in Portion_Gap.
_TOLERANCE_PORTIONS = {
    "34188": 1, "30110": 1, "6583":  1, "06583": 1,
    "15661": 1, "18363": 1, "25788": 1,
    "26364": 1, "17339": 1,
    "26214": 2,
    "22667": 2, "22668": 2, "26222": 2, "26227": 2,
    "26229": 2, "29053": 2, "30003": 2,
}
if ("Portion_Gap" in compliance.columns
        and "Status" in compliance.columns
        and "SKU" in compliance.columns):
    _tol = compliance["SKU"].astype(str).str.strip().map(_TOLERANCE_PORTIONS).fillna(0)
    _pg  = pd.to_numeric(compliance["Portion_Gap"], errors="coerce").fillna(0)
    # Match both "Deficit" (pipeline raw) and "Non-Compliant" (pipeline pre-remapped)
    _within_tol = (
        compliance["Status"].isin(["Deficit", "Non-Compliant"]) & (_pg >= -_tol)
    )
    compliance.loc[_within_tol, "Status"] = "Compliant"

# ── Compute Cases columns in dashboard (grams ÷ pack_g = cases) ──────────────
# Primary source: Pack_Qty column (g per case from pack-size parser).
# Fallback: Total_Ordered_Qty / Cases_Ordered.
# Strip values BEFORE the join to avoid whitespace mismatches.
if not bidfood_s.empty:
    _bf_pk = bidfood_s.copy()
    # Strip join keys so lookup matches compliance values
    _bf_pk["Site_Key"]     = _bf_pk["Site_Key"].astype(str).str.strip()
    _bf_pk["Product Code"] = _bf_pk["Product Code"].astype(str).str.strip()

    if "Pack_Qty" in _bf_pk.columns:
        _bf_pk["_pack_g"] = pd.to_numeric(_bf_pk["Pack_Qty"], errors="coerce")
    elif "Total_Ordered_Qty" in _bf_pk.columns and "Cases_Ordered" in _bf_pk.columns:
        _cases_ord = pd.to_numeric(_bf_pk["Cases_Ordered"],     errors="coerce")
        _tot_ord   = pd.to_numeric(_bf_pk["Total_Ordered_Qty"], errors="coerce")
        _bf_pk["_pack_g"] = _tot_ord / _cases_ord
    else:
        _bf_pk["_pack_g"] = float("nan")

    _pk_map = (
        _bf_pk.dropna(subset=["_pack_g"])
        .query("_pack_g > 0")
        .groupby(["Site_Key", "Product Code"])["_pack_g"]
        .first()
    )
    _c_keys = list(zip(
        compliance["Site_Key"].astype(str).str.strip(),
        compliance["SKU"].astype(str).str.strip(),
    ))
    _pk_vals = pd.to_numeric(
        pd.Series([_pk_map.get(k, float("nan")) for k in _c_keys], index=compliance.index),
        errors="coerce",
    )
    _valid_pk = _pk_vals.notna() & (_pk_vals > 0)
    for _raw_col, _case_col in [
        ("Required_Qty", "Cases_Required"),
        ("Ordered_Qty",  "Cases_Ordered"),
        ("Gap",          "Cases_Gap"),
    ]:
        if _raw_col in compliance.columns:
            compliance[_case_col] = (
                pd.to_numeric(compliance[_raw_col], errors="coerce") / _pk_vals
            ).round(2).where(_valid_pk)

# ── Remap Status: Surplus/Exact → "Compliant", Deficit → "Non-Compliant" ──────
if "Status" in compliance.columns:
    compliance["Status"] = compliance["Status"].map({
        "Surplus": "Compliant", "Exact": "Compliant", "Deficit": "Non-Compliant"
    }).fillna(compliance["Status"])

# ── Second tolerance pass (catches any Non-Compliant that survived the remap) ──
# Runs after Status remap so it definitively overrides with Compliant.
if ("Portion_Gap" in compliance.columns
        and "Status" in compliance.columns
        and "SKU" in compliance.columns):
    _tol2 = compliance["SKU"].astype(str).str.strip().map(_TOLERANCE_PORTIONS).fillna(0)
    _pg2  = pd.to_numeric(compliance["Portion_Gap"], errors="coerce").fillna(0)
    _has_tol = _tol2 > 0  # only apply to SKUs that have a tolerance
    _within2  = (compliance["Status"] == "Non-Compliant") & _has_tol & (_pg2 >= -_tol2)
    compliance.loc[_within2, "Status"] = "Compliant"

# ── Derived flags ──────────────────────────────────────────────────────────────
HAS_PORTIONS = ("Portion_Gap" in compliance.columns
                and pd.to_numeric(compliance["Portion_Gap"], errors="coerce").abs().sum() > 0)

# ── Site ranking ───────────────────────────────────────────────────────────────
if not site_summ.empty and "Compliance_%" in site_summ.columns:
    site_summ = (site_summ
                 .sort_values("Compliance_%", ascending=False)
                 .reset_index(drop=True))
    site_summ.insert(0, "Rank", range(1, len(site_summ) + 1))

# ── Last run banner ────────────────────────────────────────────────────────────
if not run_log.empty:
    last = run_log.iloc[-1]
    st.markdown(
        f"**Last run:** {last.get('Timestamp','–')}  &nbsp;|&nbsp;  "
        f"**Bidfood:** {last.get('Bidfood File','–')}  &nbsp;|&nbsp;  "
        f"**Items:** {last.get('Items File','–')}"
    )

# ── LKN food SKU master list with display names ────────────────────────────────
_SKU_NAME_MAP = {
    "34188": "LKN Miller Buns",
    "30110": "Skin On Fries",
    "6583":  "LKN Crispy Hot Wings",
    "06583": "LKN Crispy Hot Wings",
    "15661": "LKN Hot & Spicy Chicken Bites",
    "18363": "LKN Southern Fried Chicken Strips",
    "25788": "LKN Coated Chicken Burger",
    "26214": "LKN Mince Beef Pucks",
    "22667": "LKN Korean BBQ Sauce",
    "22668": "LKN Deluxe BBQ Sauce",
    "26222": "LKN Elite Burger Sauce",
    "26227": "LKN Honey Buffalo Sauce",
    "26229": "LKN Truffle Mayo",
    "29053": "LKN Miso Mayo",
    "30003": "LKN Ranch Sauce",
    "26364": "LKN Chick'n Burger",
    "17339": "LKN Vegan Beef",
}
_FOOD_SKUS = set(_SKU_NAME_MAP.keys())

# Week commencing selector (shown when multiple weeks available)
_wc_col_exists = "Week_Commencing" in compliance.columns
_all_weeks = []
if _wc_col_exists:
    _all_weeks = sorted(compliance["Week_Commencing"].dropna().unique().tolist(), reverse=True)

if _wc_col_exists and len(_all_weeks) >= 1:
    sel_week = st.selectbox(
        "Week commencing", _all_weeks,
        help="Filter all views to a specific week. Run the pipeline weekly to build history."
    )
    compliance   = compliance[compliance["Week_Commencing"] == sel_week]
    if "Week_Commencing" in site_summ.columns:
        site_summ = site_summ[site_summ["Week_Commencing"] == sel_week]
    if "Week_Commencing" in bidfood_s.columns:
        bidfood_s = bidfood_s[bidfood_s["Week_Commencing"] == sel_week]

st.divider()

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab1, tab2 = st.tabs(["📊 Overview", "🏪 Sites"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Overview
# ══════════════════════════════════════════════════════════════════════════════
with tab1:

    # ── KPI Row (Overview only) ────────────────────────────────────────────────
    _total_sites     = site_summ.shape[0] if not site_summ.empty else 0
    _compliant_sites = int((site_summ["Compliance_%"] >= 70).sum()) if not site_summ.empty else 0
    _noncomp_sites   = _total_sites - _compliant_sites
    _avg_comp        = round(site_summ["Compliance_%"].mean(), 1) if not site_summ.empty else 0
    _bf_spend_src    = bidfood_s.copy() if not bidfood_s.empty else pd.DataFrame()
    if not _bf_spend_src.empty and _required_sites and "Site_Key" in _bf_spend_src.columns:
        _bf_spend_src = _bf_spend_src[_bf_spend_src["Site_Key"].isin(_required_sites)]
    _bidfood_spend   = (pd.to_numeric(_bf_spend_src["Total_Spend_GBP"], errors="coerce").sum()
                        if not _bf_spend_src.empty and "Total_Spend_GBP" in _bf_spend_src.columns else 0)
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Sites Monitored",   _total_sites)
    k2.metric("Avg Compliance",    f"{_avg_comp:.1f}%")
    k3.metric("Compliant Sites",   _compliant_sites,
              delta=f"{_compliant_sites} of {_total_sites}", delta_color="normal")
    k4.metric("Non-Compliant",     _noncomp_sites,
              delta=f"{_noncomp_sites} need attention" if _noncomp_sites else "All clear",
              delta_color="inverse" if _noncomp_sites else "off")
    k5.metric("Bidfood Spend",     f"£{_bidfood_spend:,.0f}")
    st.divider()

    # ── Row 1: Donut + Top 10 Deficit SKUs ────────────────────────────────────
    c_donut, c_deficit = st.columns([1, 2])

    with c_donut:
        st.subheader("Site Compliance")
        if not site_summ.empty:
            n_comp    = int((site_summ["Compliance_%"] >= 70).sum())
            n_noncomp = int((site_summ["Compliance_%"] < 70).sum())
            _fig_donut = go.Figure(go.Pie(
                labels=["Compliant", "Non-Compliant"],
                values=[n_comp, n_noncomp],
                marker_colors=["#538135", "#C00000"],
                hole=0.60,
                textinfo="label+percent",
                hovertemplate="%{label}: %{value} sites<extra></extra>",
            ))
            _fig_donut.update_layout(
                height=300,
                margin=dict(t=10, b=0, l=0, r=0),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.15, xanchor="center", x=0.5),
                annotations=[dict(
                    text=f"<b>{_total_sites}</b><br>Sites",
                    x=0.5, y=0.5, font_size=14, showarrow=False
                )],
            )
            st.plotly_chart(_fig_donut, use_container_width=True)
        else:
            st.info("No site data available.")

    with c_deficit:
        if not compliance.empty and "Status" in compliance.columns:
            _food = compliance[compliance["SKU"].astype(str).isin(_FOOD_SKUS)].copy()
            _food["Display_Name"] = (
                _food["SKU"].astype(str).map(_SKU_NAME_MAP).fillna(_food["Ingredient"])
            )

            # Per-SKU per-site status — avoid groupby.apply() (broken in pandas 3.x)
            _food["_nc"] = (_food["Status"] == "Non-Compliant")
            _nc_flag = (
                _food.groupby(["Display_Name", "Site_Key"])["_nc"]
                .any()
                .reset_index()
            )
            _nc_flag["Site_Status"] = _nc_flag["_nc"].map(
                {True: "Non-Compliant", False: "Compliant"}
            )
            _sku_site = _nc_flag[["Display_Name", "Site_Key", "Site_Status"]]

            def _pct_sites(label):
                _tmp = _sku_site.copy()
                _tmp["_match"] = (_tmp["Site_Status"] == label).astype(int)
                _agg = (
                    _tmp.groupby("Display_Name")
                    .agg(_sum=("_match", "sum"), _tot=("_match", "count"))
                    .reset_index()
                )
                _agg["Pct"] = (_agg["_sum"] / _agg["_tot"] * 100).round(1)
                return _agg[["Display_Name", "Pct"]]

            _nc_grp   = _pct_sites("Non-Compliant")
            _comp_grp = _pct_sites("Compliant")
            _nc5   = _nc_grp.nlargest(5, "Pct").sort_values("Pct", ascending=True)
            _comp5 = _comp_grp.nlargest(5, "Pct").sort_values("Pct", ascending=True)

            _nc_col, _comp_col = st.columns(2)

            # ── Top 5 Non-Compliant SKUs (% of sites) ─────────────────────────
            with _nc_col:
                st.markdown("**🔴 Top 5 Non-Compliant SKUs (% of sites)**")
                if not _nc5.empty:
                    _fig_nc = px.bar(
                        _nc5, x="Pct", y="Display_Name", orientation="h",
                        color_discrete_sequence=["#C00000"],
                        text=[f"{v:.1f}%" for v in _nc5["Pct"]],
                        labels={"Pct": "Sites Non-Compliant %", "Display_Name": ""},
                    )
                    _fig_nc.update_traces(textposition="outside")
                    _fig_nc.update_layout(
                        height=250, margin=dict(t=10, b=0, l=0, r=20),
                        xaxis=dict(range=[0, 135], fixedrange=True),
                    )
                    st.plotly_chart(_fig_nc, use_container_width=True)
                else:
                    st.success("No non-compliant SKUs!")

            # ── Top 5 Compliant SKUs (% of sites) ─────────────────────────────
            with _comp_col:
                st.markdown("**🟢 Top 5 Compliant SKUs (% of sites)**")
                if not _comp5.empty:
                    _fig_comp = px.bar(
                        _comp5, x="Pct", y="Display_Name", orientation="h",
                        color_discrete_sequence=["#538135"],
                        text=[f"{v:.1f}%" for v in _comp5["Pct"]],
                        labels={"Pct": "Sites Compliant %", "Display_Name": ""},
                    )
                    _fig_comp.update_traces(textposition="outside")
                    _fig_comp.update_layout(
                        height=250, margin=dict(t=10, b=0, l=0, r=20),
                        xaxis=dict(range=[0, 135], fixedrange=True),
                    )
                    st.plotly_chart(_fig_comp, use_container_width=True)
                else:
                    st.info("No compliant SKUs found.")

    st.divider()

    # ── Row 2: Top 10 / Bottom 10 sites ───────────────────────────────────────
    if not site_summ.empty and "Compliance_%" in site_summ.columns:
        c_top, c_bot = st.columns(2)

        with c_top:
            st.subheader("🏆 Top 10 Compliant Sites")
            _top10 = (site_summ.nlargest(10, "Compliance_%")
                      .sort_values("Compliance_%", ascending=True).copy())
            _top10["label"] = [f"{x:.1f}%" for x in _top10["Compliance_%"]]
            _fig_top = px.bar(
                _top10, x="Compliance_%", y="Store_Name",
                orientation="h",
                color="Compliance_%",
                color_continuous_scale=[[0, "#92D050"], [1, "#375623"]],
                text="label",
                labels={"Compliance_%": "Compliance %", "Store_Name": ""},
            )
            _fig_top.update_traces(textposition="outside")
            _fig_top.update_layout(
                height=380,
                margin=dict(t=0, b=0, l=0, r=70),
                coloraxis_showscale=False,
                xaxis=dict(range=[0, 115], fixedrange=True),
            )
            st.plotly_chart(_fig_top, use_container_width=True)

        with c_bot:
            st.subheader("⚠️ Bottom 10 Sites")
            _bot10 = (site_summ.nsmallest(10, "Compliance_%")
                      .sort_values("Compliance_%", ascending=False).copy())
            _bot10["label"] = [f"{x:.1f}%" for x in _bot10["Compliance_%"]]
            _fig_bot = px.bar(
                _bot10, x="Compliance_%", y="Store_Name",
                orientation="h",
                color_discrete_sequence=["#C00000"],
                text="label",
                labels={"Compliance_%": "Compliance %", "Store_Name": ""},
            )
            _fig_bot.update_traces(textposition="outside")
            _fig_bot.update_layout(
                height=380,
                margin=dict(t=0, b=0, l=0, r=70),
                xaxis=dict(range=[0, 115], fixedrange=True),
            )
            st.plotly_chart(_fig_bot, use_container_width=True)

    st.divider()

    # ── Full site ranking table + download ─────────────────────────────────────
    st.subheader("All Sites — Compliance Ranking")
    if not site_summ.empty:
        def _pct_color(val):
            try:
                pct = float(str(val).replace("%", ""))
                if pct >= 80:   return "color: #266F00; font-weight:700"
                elif pct >= 50: return "color: #B8860B; font-weight:700"
                else:           return "color: #C00000; font-weight:700"
            except Exception:
                return ""

        _disp = site_summ.copy()
        # Merge Surplus + Exact → Compliant SKUs
        if "Surplus" in _disp.columns and "Exact" in _disp.columns:
            _disp["Compliant SKUs"] = _disp["Surplus"] + _disp["Exact"]
        elif "Surplus" in _disp.columns:
            _disp["Compliant SKUs"] = _disp["Surplus"]
        # Select and rename columns for display
        _col_map = {
            "Rank":           "Rank",
            "Store_Name":     "Store",
            "Compliant SKUs": "Compliant SKUs",
            "Deficit":        "Non-Compliant SKUs",
            "Total_SKUs":     "Total SKUs",
            "Compliance_%":   "Compliance %",
        }
        _disp = _disp[[c for c in _col_map if c in _disp.columns]].rename(columns=_col_map)
        _int_cols = [c for c in ["Rank","Compliant SKUs","Non-Compliant SKUs","Total SKUs"] if c in _disp.columns]
        _fmt = {c: "{:.0f}" for c in _int_cols}
        if "Compliance %" in _disp.columns:
            _fmt["Compliance %"] = "{:.1f}%"
        _styled = (
            _disp.style
            .format(_fmt)
            .map(_pct_color, subset=["Compliance %"] if "Compliance %" in _disp.columns else [])
            .hide(axis="index")
        )
        st.dataframe(_styled, use_container_width=True, height=400)

        # Download — original numeric df with Rank
        st.download_button(
            "⬇️ Download Full Site Ranking (CSV)",
            data=_to_csv(site_summ),
            file_name="lkn_site_compliance_ranking.csv",
            mime="text/csv",
        )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Sites detail
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.subheader("Site-level Compliance Detail")

    if not compliance.empty:
        _sku_col = "SKU" if "SKU" in compliance.columns else compliance.columns[2]

        # Build combined SKU — Ingredient options (one label per unique SKU using first ingredient name)
        _sku_ingr_map = (
            compliance.dropna(subset=[_sku_col, "Ingredient"])
            .astype({_sku_col: str, "Ingredient": str})
            .groupby(_sku_col)["Ingredient"]
            .first()
            .to_dict()
        )
        # Use canonical names from SKU map where available
        _sku_labels = sorted([
            f"{sku} — {_SKU_NAME_MAP.get(sku, _sku_ingr_map.get(sku, sku))}"
            for sku in _sku_ingr_map
        ])
        _label_to_sku = {lbl: lbl.split(" — ")[0] for lbl in _sku_labels}

        # Account Manager filter — built from mapping sheet
        _acct_mgr_col_exists = bool(_site_key_to_manager)
        if _acct_mgr_col_exists:
            # Map Site_Key → Account Manager for every row in compliance
            _compliance_mgr = compliance["Site_Key"].astype(str).map(_site_key_to_manager).fillna("Unassigned")
            _all_managers = sorted(_compliance_mgr.dropna().unique().tolist())
        else:
            _compliance_mgr = pd.Series(["Unassigned"] * len(compliance), index=compliance.index)
            _all_managers = []

        _frow1_cols = st.columns(4) if _acct_mgr_col_exists else st.columns(3)
        with _frow1_cols[0]:
            if _acct_mgr_col_exists:
                _sel_managers = st.multiselect(
                    "Account Manager", _all_managers,
                    placeholder="Filter by account manager…",
                    key="sites_manager_filter",
                )
            else:
                _sel_managers = []

        # Scope Store dropdown to selected manager's sites only
        if _sel_managers:
            _mgr_sites_for_store = {sk for sk, mgr in _site_key_to_manager.items() if mgr in _sel_managers}
            _stores_for_mgr = sorted(
                compliance[compliance["Site_Key"].astype(str).isin(_mgr_sites_for_store)]
                ["Store_Name"].dropna().astype(str).unique().tolist()
            )
        else:
            _stores_for_mgr = sorted(compliance["Store_Name"].dropna().astype(str).unique().tolist())

        with _frow1_cols[1 if _acct_mgr_col_exists else 0]:
            _sel_stores = st.multiselect(
                "Store", _stores_for_mgr,
                placeholder="Type store name…",
                key="sites_store_filter",
            )
        with _frow1_cols[2 if _acct_mgr_col_exists else 1]:
            _sel_status = st.selectbox(
                "Status", ["All", "Compliant", "Non-Compliant"],
                key="sites_status_filter",
            )
        with _frow1_cols[3 if _acct_mgr_col_exists else 2]:
            _sel_sku_ingr = st.multiselect(
                "SKU / Ingredient", _sku_labels,
                placeholder="Search by SKU or ingredient…",
                key="sites_sku_ingredient_filter",
            )

        _disp2 = compliance.copy()
        if _sel_managers:
            _mgr_sites = {sk for sk, mgr in _site_key_to_manager.items() if mgr in _sel_managers}
            _disp2 = _disp2[_disp2["Site_Key"].astype(str).isin(_mgr_sites)]
        if _sel_stores:
            _disp2 = _disp2[_disp2["Store_Name"].astype(str).isin(_sel_stores)]
        if _sel_status != "All":
            _disp2 = _disp2[_disp2["Status"] == _sel_status]
        if _sel_sku_ingr:
            _sel_skus = [_label_to_sku[l] for l in _sel_sku_ingr if l in _label_to_sku]
            _disp2 = _disp2[_disp2[_sku_col].astype(str).isin(_sel_skus)]

        # ── Site-level KPIs — only shown when a filter is active ──────────────
        _is_filtered = bool(_sel_managers or _sel_stores)
        if _is_filtered:
            _filtered_site_keys = _disp2["Site_Key"].astype(str).unique() if "Site_Key" in _disp2.columns else []
            _ss_filtered = (
                site_summ[site_summ["Site_Key"].astype(str).isin(_filtered_site_keys)].copy()
                if not site_summ.empty and len(_filtered_site_keys) > 0
                else pd.DataFrame()
            )
            _f_total   = _ss_filtered.shape[0]
            _f_comp    = int((_ss_filtered["Compliance_%"] >= 70).sum()) if not _ss_filtered.empty else 0
            _f_noncomp = _f_total - _f_comp
            _f_avg     = round(_ss_filtered["Compliance_%"].mean(), 1) if not _ss_filtered.empty else 0
            _f_bf = bidfood_s.copy() if not bidfood_s.empty else pd.DataFrame()
            if not _f_bf.empty and "Site_Key" in _f_bf.columns and len(_filtered_site_keys) > 0:
                _f_bf = _f_bf[_f_bf["Site_Key"].astype(str).isin(_filtered_site_keys)]
            _f_spend = (
                pd.to_numeric(_f_bf["Total_Spend_GBP"], errors="coerce").sum()
                if not _f_bf.empty and "Total_Spend_GBP" in _f_bf.columns else 0
            )
            _panel_label = (
                f"Account Manager: {', '.join(_sel_managers)}" if _sel_managers
                else "Store filter active"
            )
            st.divider()
            st.caption(f"📊 KPIs for: **{_panel_label}**")
            _mk1, _mk2, _mk3, _mk4, _mk5 = st.columns(5)
            _mk1.metric("Sites",            _f_total)
            _mk2.metric("Avg Compliance",   f"{_f_avg:.1f}%")
            _mk3.metric("Compliant (≥70%)", _f_comp,
                        delta=f"{_f_comp} of {_f_total}", delta_color="normal")
            _mk4.metric("Non-Compliant",    _f_noncomp,
                        delta=f"{_f_noncomp} need attention" if _f_noncomp else "All clear",
                        delta_color="inverse" if _f_noncomp else "off")
            _mk5.metric("Bidfood Spend",    f"£{_f_spend:,.0f}")
            st.divider()

        def _status_bg(val):
            _c = {"Compliant": "#E5F5E0", "Non-Compliant": "#FFE8E8"}
            return f"background-color: {_c.get(val, '')}"

        # ── Column visibility ──────────────────────────────────────────────────
        # _PERM_HIDDEN: never shown, not available in the toggle.
        # _HIDDEN_COLS: hidden by default but user can add them via the toggle.
        _PERM_HIDDEN = {"Site_Key", "Carry_Forward_Portions"}
        _HIDDEN_COLS = {"Required_Qty", "Req_UOM", "Ordered_Qty", "Ord_UOM", "Gap",
                        "Opening_Stock_g", "Closing_Stock_g", "Week_Commencing"}
        _COL_RENAME  = {
            "Portion_Required":       "Portions Used (Units)",
            "Portion_Ordered":        "Portions Ordered (Units)",
            "Portion_Gap":            "Ordered vs Used (Variance) (Units)",
            "Carry_Forward_Portions": "Stock Carried Forward (Units)",
            "Cases_Required":         "Portions Used (Cases)",
            "Cases_Ordered":          "Portions Ordered (Cases)",
            "Cases_Gap":              "Ordered vs Used (Variance) (Cases)",
            "Status":                 "Compliance Status",
        }
        _all_possible = [c for c in
                         ["Week_Commencing", "Site_Key", "Store_Name", "SKU", "Ingredient",
                          "Required_Qty", "Req_UOM", "Ordered_Qty", "Ord_UOM",
                          "Opening_Stock_g", "Gap", "Closing_Stock_g",
                          "Portion_Required", "Cases_Required",
                          "Portion_Ordered",  "Cases_Ordered",
                          "Portion_Gap",      "Cases_Gap",
                          "Carry_Forward_Portions", "Status"]
                         if c in _disp2.columns and c not in _PERM_HIDDEN]

        # Display names for the toggle UI (same as _COL_RENAME where applicable)
        _col_display = {c: _COL_RENAME.get(c, c) for c in _all_possible}
        _display_to_raw = {v: k for k, v in _col_display.items()}

        # Default visible = everything except the hidden set; Status (renamed) always last
        _st_display = _col_display.get("Status", "Status")
        _default_visible_display = [
            _col_display[c] for c in _all_possible
            if c not in _HIDDEN_COLS and c != "Status"
        ] + ([_st_display] if "Status" in _all_possible else [])

        # Session-state key: initialise from URL query params (survives page refresh)
        # then sanitize any stale column names after a code deploy.
        _valid_display_names = set(_col_display[c] for c in _all_possible)

        if "sites_col_toggle" not in st.session_state:
            # First render: try to restore from ?cols=... query param
            _qp_raw = st.query_params.get("cols", "")
            _from_qp = [c for c in _qp_raw.split(",") if c in _valid_display_names] if _qp_raw else []
            st.session_state["sites_col_toggle"] = _from_qp if _from_qp else _default_visible_display
        else:
            # Subsequent renders: drop any stale names (column renames between deploys)
            _clean = [d for d in st.session_state["sites_col_toggle"] if d in _valid_display_names]
            st.session_state["sites_col_toggle"] = _clean if _clean else _default_visible_display

        def _save_col_prefs():
            _sel = st.session_state.get("sites_col_toggle", [])
            if _sel:
                st.query_params["cols"] = ",".join(_sel)
            elif "cols" in st.query_params:
                del st.query_params["cols"]

        with st.expander("⚙️ Show / Hide Columns"):
            st.caption("Column choices are saved in your browser URL — they survive a page refresh.")
            _toggled_display = st.multiselect(
                "Visible columns",
                options=[_col_display[c] for c in _all_possible],
                key="sites_col_toggle",
                on_change=_save_col_prefs,
            )

        # Map back to raw column names; only keep valid names; Status always last
        _toggled_display = [d for d in _toggled_display if d in _display_to_raw]
        _show_cols_raw = [_display_to_raw[d] for d in _toggled_display if d != _st_display]
        if "Status" in [_display_to_raw.get(d) for d in _toggled_display]:
            _show_cols_raw.append("Status")
        _show_cols = _show_cols_raw or ["SKU", "Ingredient", "Status"]  # fallback

        # Apply display rename
        _disp2_show = _disp2[_show_cols].rename(columns={
            k: v for k, v in _COL_RENAME.items() if k in _show_cols
        })
        _show_renamed = list(_disp2_show.columns)

        _num_cols_1dp = [c for c in
                         ["Required_Qty", "Ordered_Qty", "Gap"] if c in _show_renamed]
        # Units columns → 0 dp; Cases columns → 2 dp
        _unit_cols = [_COL_RENAME.get(c, c) for c in
                      ["Portion_Required", "Portion_Ordered", "Portion_Gap", "Carry_Forward_Portions"]]
        _case_cols = [_COL_RENAME.get(c, c) for c in
                      ["Cases_Required", "Cases_Ordered", "Cases_Gap"]]
        _num_cols_0dp = [c for c in _unit_cols if c in _show_renamed]
        _num_cols_2dp = [c for c in _case_cols if c in _show_renamed]
        _detail_fmt = {c: "{:.1f}" for c in _num_cols_1dp}
        _detail_fmt.update({c: "{:.0f}" for c in _num_cols_0dp})
        _detail_fmt.update({c: "{:.2f}" for c in _num_cols_2dp})
        _detail_styled = _disp2_show.style.format(_detail_fmt, na_rep="—")
        _status_disp = _COL_RENAME.get("Status", "Status") if "Status" in _show_cols else None
        if _status_disp and _status_disp in _disp2_show.columns:
            _detail_styled = _detail_styled.map(_status_bg, subset=[_status_disp])
        _col_help = {
            "SKU":                                    "Unique product code assigned by Bidfood for this ingredient.",
            "Ingredient":                             "Ingredient/product name as listed in the recipe or menu spec.",
            "Portions Used (Units)":                  "Number of portions consumed at this site based on sales/recipe data.",
            "Portions Ordered (Units)":               "Number of portions ordered from Bidfood for this ingredient.",
            "Ordered vs Used (Variance) (Units)":     "Ordered minus Used in portions. Positive = surplus, Negative = shortfall.",
            "Stock Carried Forward (Units)":          "Portions remaining in stock carried over from the previous period.",
            "Portions Used (Cases)":                  "Portions used expressed as cases (packs) — Units ÷ pack size.",
            "Portions Ordered (Cases)":               "Portions ordered expressed as cases (packs) — Units ÷ pack size.",
            "Ordered vs Used (Variance) (Cases)":     "Variance expressed as cases. Positive = surplus cases, Negative = shortfall cases.",
            "Compliance Status":                      "Compliant = ordering meets or exceeds usage. Non-Compliant = shortfall with no carried-forward stock to cover it.",
        }
        _col_config = {
            col: st.column_config.TextColumn(col, help=tip)
            for col, tip in _col_help.items()
            if col in _disp2_show.columns
        }
        st.dataframe(_detail_styled, use_container_width=True, height=520, column_config=_col_config)
        st.caption(f"{len(_disp2):,} rows shown")

        st.download_button(
            "⬇️ Download Filtered Results (CSV)",
            data=_to_csv(_disp2_show),
            file_name="lkn_compliance_detail.csv",
            mime="text/csv",
            key="sites_download",
        )

        # ── When exactly one SKU is selected, show ingredient + bidfood detail ──
        _sel_skus = [_label_to_sku[l] for l in _sel_sku_ingr if l in _label_to_sku]
        if len(_sel_skus) == 1:
            _sel_sku_single = _sel_skus[0]
            st.divider()
            st.markdown(f"#### SKU {_sel_sku_single} — Detailed Breakdown")
            _ing_sku, _bid_sku = st.columns(2)

            with _ing_sku:
                st.markdown("**Ingredient Requirements (System B)**")
                if not ingredient_s.empty and "SKU" in ingredient_s.columns:
                    _ing_f = ingredient_s[ingredient_s["SKU"].astype(str) == _sel_sku_single]
                    if not _ing_f.empty:
                        if "Total_Raw_Qty" in _ing_f.columns:
                            _ing_f = _ing_f.sort_values("Total_Raw_Qty", ascending=False)
                        st.dataframe(_ing_f, use_container_width=True, height=280)
                        st.caption(f"{len(_ing_f):,} rows")
                    else:
                        st.info("No ingredient rows for this SKU.")
                else:
                    st.info("No ingredient data loaded.")

            with _bid_sku:
                st.markdown("**Bidfood Stock Ordered (System A)**")
                if not bidfood_s.empty and "Product Code" in bidfood_s.columns:
                    _bid_f = bidfood_s[bidfood_s["Product Code"].astype(str) == _sel_sku_single]
                    if not _bid_f.empty:
                        if "Total_Ordered_Qty" in _bid_f.columns:
                            _bid_f = _bid_f.copy()
                            _bid_f["Total_Ordered_Qty"] = pd.to_numeric(
                                _bid_f["Total_Ordered_Qty"], errors="coerce")
                            _bid_f = _bid_f.sort_values("Total_Ordered_Qty", ascending=False)
                        st.dataframe(_bid_f, use_container_width=True, height=280)
                        st.caption(f"{len(_bid_f):,} rows")
                    else:
                        st.info("No Bidfood orders for this SKU.")
                else:
                    st.info("No Bidfood stock data loaded.")
