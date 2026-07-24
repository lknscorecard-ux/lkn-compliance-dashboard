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
    sid = (st.secrets.get("RESULTS_SHEET_ID")
           or os.environ.get("RESULTS_SHEET_ID", RESULTS_SHEET_ID))
    gc  = _get_gc()
    sh  = gc.open_by_key(sid)
    out = {}
    for tab in ["Compliance Gap", "Site Summary",
                "Ingredient Requirements", "Bidfood Stock", "Run Log"]:
        try:
            recs       = sh.worksheet(tab).get_all_records()
            out[tab]   = pd.DataFrame(recs) if recs else pd.DataFrame()
        except Exception:
            out[tab]   = pd.DataFrame()
        time.sleep(1)   # respect Sheets API 60 reads/min quota
    return out

def _safe(tab: str) -> pd.DataFrame:
    return _load_all_sheets().get(tab, pd.DataFrame())

def _to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

# ── Header ─────────────────────────────────────────────────────────────────────
h1, h2, h3 = st.columns([3, 1, 1])
with h1:
    st.title("🍗 LKN Compliance Dashboard")
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
    compliance   = _safe("Compliance Gap")
    site_summ    = _safe("Site Summary")
    ingredient_s = _safe("Ingredient Requirements")
    bidfood_s    = _safe("Bidfood Stock")
    run_log      = _safe("Run Log")

if compliance.empty:
    st.info("No compliance data yet. Trigger the pipeline above.")
    st.stop()

# ── Numeric coercion ───────────────────────────────────────────────────────────
_num_compliance = ["Required_Qty", "Ordered_Qty", "Gap",
                   "Portion_Required", "Portion_Ordered", "Portion_Gap"]
for c in _num_compliance:
    if c in compliance.columns:
        compliance[c] = pd.to_numeric(compliance[c], errors="coerce").fillna(0)

_num_site = ["Compliance_%", "Total_SKUs", "Surplus", "Deficit", "Exact"]
for c in _num_site:
    if c in site_summ.columns:
        site_summ[c] = pd.to_numeric(site_summ[c], errors="coerce").fillna(0)

for c in ["Total_Raw_Qty", "Total_Cost"]:
    if c in ingredient_s.columns:
        ingredient_s[c] = pd.to_numeric(ingredient_s[c], errors="coerce").fillna(0)

# ── Remap Status: Surplus/Exact → "Compliant", Deficit → "Non-Compliant" ──────
if "Status" in compliance.columns:
    compliance["Status"] = compliance["Status"].map({
        "Surplus": "Compliant", "Exact": "Compliant", "Deficit": "Non-Compliant"
    }).fillna(compliance["Status"])

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
}
_FOOD_SKUS = set(_SKU_NAME_MAP.keys())

# Week commencing selector (shown when multiple weeks available)
_wc_col_exists = "Week_Commencing" in compliance.columns
_all_weeks = []
if _wc_col_exists:
    _all_weeks = sorted(compliance["Week_Commencing"].dropna().unique().tolist(), reverse=True)

if _wc_col_exists and len(_all_weeks) > 1:
    sel_week = st.selectbox(
        "Week commencing", _all_weeks,
        help="Filter all views to a specific week. Run the pipeline weekly to build history."
    )
    compliance   = compliance[compliance["Week_Commencing"] == sel_week]
    if "Week_Commencing" in site_summ.columns:
        site_summ = site_summ[site_summ["Week_Commencing"] == sel_week]
elif _wc_col_exists and len(_all_weeks) == 1:
    st.caption(f"Week commencing: **{_all_weeks[0]}**")

st.divider()

# ── KPI Row ────────────────────────────────────────────────────────────────────
_total_sites     = site_summ.shape[0] if not site_summ.empty else 0
_compliant_sites = int((site_summ["Deficit"] == 0).sum()) if not site_summ.empty else 0
_noncomp_sites   = _total_sites - _compliant_sites
_avg_comp        = round(site_summ["Compliance_%"].mean(), 1) if not site_summ.empty else 0
_bidfood_spend   = (bidfood_s["Total_Spend_GBP"]
                    .apply(pd.to_numeric, errors="coerce").sum()
                    if not bidfood_s.empty else 0)

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

# ── Tabs (Recipe + Run Log removed) ───────────────────────────────────────────
tab1, tab2 = st.tabs(["📊 Overview", "🏪 Sites"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Overview
# ══════════════════════════════════════════════════════════════════════════════
with tab1:

    # ── Row 1: Donut + Top 10 Deficit SKUs ────────────────────────────────────
    c_donut, c_deficit = st.columns([1, 2])

    with c_donut:
        st.subheader("Site Compliance")
        if not site_summ.empty:
            n_comp    = int((site_summ["Deficit"] == 0).sum())
            n_noncomp = int((site_summ["Deficit"] > 0).sum())
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

            # Per-SKU per-site status (one pass, used by both charts)
            _sku_site = (
                _food.groupby(["Display_Name", "Site_Key"])["Status"]
                .apply(lambda s: "Non-Compliant" if (s == "Non-Compliant").any() else "Compliant")
                .reset_index()
                .rename(columns={"Status": "Site_Status"})
            )

            def _pct_sites(grp, label):
                return (
                    _sku_site.groupby("Display_Name")
                    .apply(lambda x: round((x["Site_Status"] == label).sum() / len(x) * 100, 1))
                    .reset_index()
                    .rename(columns={0: "Pct"})
                )

            _nc_grp   = _pct_sites(_sku_site, "Non-Compliant")
            _comp_grp = _pct_sites(_sku_site, "Compliant")
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
                        text=_nc5["Pct"].apply(lambda v: f"{v:.1f}%"),
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
                        text=_comp5["Pct"].apply(lambda v: f"{v:.1f}%"),
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
            _top10["label"] = _top10["Compliance_%"].apply(lambda x: f"{x:.1f}%")
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
            _bot10["label"] = _bot10["Compliance_%"].apply(lambda x: f"{x:.1f}%")
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
        _int_cols   = [c for c in ["Rank","Deficit","Surplus","Exact","Total_SKUs"] if c in _disp.columns]
        _fmt = {c: "{:.0f}" for c in _int_cols}
        if "Compliance_%" in _disp.columns:
            _fmt["Compliance_%"] = "{:.1f}%"
        _styled = (
            _disp.style
            .format(_fmt)
            .map(_pct_color, subset=["Compliance_%"] if "Compliance_%" in _disp.columns else [])
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
        _f1, _f2, _f3 = st.columns(3)
        with _f1:
            _sites = (["All"]
                      + sorted(compliance["Site_Key"].dropna().astype(str).unique().tolist()))
            _sel_site = st.selectbox("Site", _sites, key="sites_site_filter")
        with _f2:
            _sel_status = st.selectbox("Status", ["All", "Compliant", "Non-Compliant"],
                                       key="sites_status_filter")
        with _f3:
            _sku_col  = "SKU" if "SKU" in compliance.columns else compliance.columns[2]
            _all_skus = sorted(compliance[_sku_col].dropna().astype(str).unique().tolist())
            _sel_skus = st.multiselect("SKU", _all_skus,
                                       placeholder="All SKUs",
                                       key="sites_sku_filter")

        _disp2 = compliance.copy()
        if _sel_site  != "All": _disp2 = _disp2[_disp2["Site_Key"] == _sel_site]
        if _sel_status != "All": _disp2 = _disp2[_disp2["Status"]   == _sel_status]
        if _sel_skus:            _disp2 = _disp2[_disp2[_sku_col].astype(str).isin(_sel_skus)]

        def _status_bg(val):
            _c = {"Compliant": "#E5F5E0", "Non-Compliant": "#FFE8E8"}
            return f"background-color: {_c.get(val, '')}"

        # ── Columns shown globally (change in code to update for all users) ──
        # Hidden: raw qty/UOM columns + Week_Commencing
        _HIDDEN_COLS = {"Required_Qty", "Req_UOM", "Ordered_Qty", "Ord_UOM", "Gap",
                        "Week_Commencing"}
        _COL_RENAME  = {
            "Portion_Required": "Portion_Required (Consumed)",
            "Portion_Ordered":  "Portion_Ordered (Bidfood)",
        }
        _all_possible = [c for c in
                         ["Week_Commencing", "Site_Key", "Store_Name", "SKU", "Ingredient",
                          "Required_Qty", "Req_UOM", "Ordered_Qty", "Ord_UOM", "Gap",
                          "Portion_Required", "Portion_Ordered", "Portion_Gap", "Status"]
                         if c in _disp2.columns]

        # Display names for the toggle UI (same as _COL_RENAME where applicable)
        _col_display = {c: _COL_RENAME.get(c, c) for c in _all_possible}
        _display_to_raw = {v: k for k, v in _col_display.items()}

        # Default visible = everything except the hidden set; Status always last
        _default_visible_display = [
            _col_display[c] for c in _all_possible
            if c not in _HIDDEN_COLS and c != "Status"
        ] + (["Status"] if "Status" in _all_possible else [])

        # Session-state key: initialise once per session
        if "sites_col_toggle" not in st.session_state:
            st.session_state["sites_col_toggle"] = _default_visible_display

        with st.expander("⚙️ Show / Hide Columns"):
            st.caption("Changes apply for your session. Ask admin to change the default for everyone.")
            _toggled_display = st.multiselect(
                "Visible columns",
                options=[_col_display[c] for c in _all_possible],
                default=st.session_state["sites_col_toggle"],
                key="sites_col_toggle",
            )

        # Map back to raw column names; Status always last
        _show_cols_raw = [_display_to_raw[d] for d in _toggled_display if d != "Status"]
        if "Status" in [_display_to_raw.get(d) for d in _toggled_display]:
            _show_cols_raw.append("Status")
        _show_cols = _show_cols_raw

        # Apply display rename
        _disp2_show = _disp2[_show_cols].rename(columns={
            k: v for k, v in _COL_RENAME.items() if k in _show_cols
        })
        _show_renamed = list(_disp2_show.columns)

        _num_cols_1dp = [c for c in
                         ["Required_Qty", "Ordered_Qty", "Gap"] if c in _show_renamed]
        _pr_col = _COL_RENAME.get("Portion_Required", "Portion_Required")
        _po_col = _COL_RENAME.get("Portion_Ordered",  "Portion_Ordered")
        _num_cols_0dp = [c for c in
                         [_pr_col, _po_col, "Portion_Gap"] if c in _show_renamed]
        _detail_fmt = {c: "{:.1f}" for c in _num_cols_1dp}
        _detail_fmt.update({c: "{:.0f}" for c in _num_cols_0dp})
        _detail_styled = _disp2_show.style.format(_detail_fmt)
        _status_disp = "Status" if "Status" in _show_renamed else None
        if _status_disp:
            _detail_styled = _detail_styled.map(_status_bg, subset=[_status_disp])
        st.dataframe(_detail_styled, use_container_width=True, height=520)
        st.caption(f"{len(_disp2):,} rows shown")

        st.download_button(
            "⬇️ Download Filtered Results (CSV)",
            data=_to_csv(_disp2_show),
            file_name="lkn_compliance_detail.csv",
            mime="text/csv",
            key="sites_download",
        )

        # ── When exactly one SKU is selected, show ingredient + bidfood detail ──
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
