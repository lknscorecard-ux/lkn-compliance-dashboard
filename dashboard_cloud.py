"""
dashboard_cloud.py  —  LKN Compliance Dashboard (Cloud Version)
================================================================
Permanently hosted on Streamlit Community Cloud.
Reads live from Google Sheets — no file uploads, no local Python needed.
Auto-refreshes every 5 minutes.

Deploy: connect your GitHub repo to https://share.streamlit.io
Set secrets: GOOGLE_CREDENTIALS_JSON and RESULTS_SHEET_ID

Anyone with the URL can view the dashboard.
"""

import os, json, warnings
warnings.filterwarnings("ignore")

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from google.oauth2.service_account import Credentials
import gspread

# ── Config ─────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LKN Compliance Dashboard",
    page_icon="🍗",
    layout="wide",
    initial_sidebar_state="collapsed",
)

RESULTS_SHEET_ID = os.environ.get("RESULTS_SHEET_ID", "")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

st.markdown("""
<style>
  [data-testid="stMetricValue"] { font-size: 2.2rem; font-weight: 700; }
  [data-testid="stMetricLabel"] { font-size: 0.85rem; color: #666; }
  .stDataFrame { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# ── Auth ────────────────────────────────────────────────────────────────────────
@st.cache_resource(ttl=3600)
def _get_gc():
    try:
        raw = st.secrets.get("GOOGLE_CREDENTIALS_JSON") or os.environ.get("GOOGLE_CREDENTIALS_JSON","")
        if isinstance(raw, str) and raw.strip():
            info = json.loads(raw)
        elif hasattr(raw, "_asdict"):      # Streamlit AttrDict from secrets.toml
            info = dict(raw)
        else:
            info = raw
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception:
        path = os.path.join(os.path.dirname(__file__), "google_credentials.json")
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return gspread.authorize(creds)


@st.cache_data(ttl=300, show_spinner="Refreshing data …")
def _load(tab: str) -> pd.DataFrame:
    sid = (
        st.secrets.get("RESULTS_SHEET_ID")
        or os.environ.get("RESULTS_SHEET_ID", RESULTS_SHEET_ID)
    )
    gc = _get_gc()
    ws = gc.open_by_key(sid).worksheet(tab)
    records = ws.get_all_records()
    return pd.DataFrame(records) if records else pd.DataFrame()


def _load_safe(tab):
    try:
        return _load(tab)
    except Exception as e:
        st.warning(f"Could not load '{tab}': {e}")
        return pd.DataFrame()

# ── Header ──────────────────────────────────────────────────────────────────────
col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
with col_h1:
    st.title("🍗 LKN Compliance Dashboard")
with col_h2:
    if st.button("🔄 Refresh Now"):
        st.cache_data.clear()
        st.rerun()
with col_h3:
    st.markdown("<br>", unsafe_allow_html=True)
    st.caption("Auto-refreshes every 5 min")

# ── Load all sheets ─────────────────────────────────────────────────────────────
compliance    = _load_safe("Compliance Gap")
site_summ     = _load_safe("Site Summary")
ingredient_s  = _load_safe("Ingredient Requirements")
bidfood_s     = _load_safe("Bidfood Stock")
recipe_s      = _load_safe("Recipe Summary")
unmatched_s   = _load_safe("Unmatched")
run_log       = _load_safe("Run Log")

if compliance.empty:
    st.info("No compliance data yet. The pipeline runs every Monday — or trigger it manually from Google Cloud Console.")
    st.stop()

# Numeric coercion
for col in ["Required_Qty","Ordered_Qty","Gap"]:
    if col in compliance.columns:
        compliance[col] = pd.to_numeric(compliance[col], errors="coerce").fillna(0)
for col in ["Compliance_%","Total_SKUs","Surplus","Deficit","Exact"]:
    if col in site_summ.columns:
        site_summ[col] = pd.to_numeric(site_summ[col], errors="coerce").fillna(0)
for col in ["Total_Raw_Qty","Total_Cost"]:
    if col in ingredient_s.columns:
        ingredient_s[col] = pd.to_numeric(ingredient_s[col], errors="coerce").fillna(0)

# ── Last run info ────────────────────────────────────────────────────────────────
if not run_log.empty:
    last = run_log.iloc[-1]
    st.markdown(
        f"**Last run:** {last.get('Timestamp','–')}  &nbsp;|&nbsp;  "
        f"**Bidfood:** {last.get('Bidfood File','–')}  &nbsp;|&nbsp;  "
        f"**Items:** {last.get('Items File','–')}"
    )
st.divider()

# ── KPI Row ──────────────────────────────────────────────────────────────────────
total_sites    = site_summ.shape[0] if not site_summ.empty else "–"
avg_compliance = round(site_summ["Compliance_%"].mean(), 1) if not site_summ.empty else 0
deficit_sites  = int((site_summ["Deficit"] > 0).sum()) if not site_summ.empty else 0
total_cost     = ingredient_s["Total_Cost"].sum() if "Total_Cost" in ingredient_s.columns else 0
total_ordered  = bidfood_s["Total_Spend_GBP"].apply(pd.to_numeric, errors="coerce").sum() if not bidfood_s.empty else 0

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Sites Monitored",    total_sites)
k2.metric("Avg Compliance",     f"{avg_compliance:.1f}%")
k3.metric("Sites with Deficit", deficit_sites,
          delta=f"{deficit_sites} need attention" if deficit_sites else "All compliant",
          delta_color="inverse" if deficit_sites else "off")
k4.metric("Ingredient Cost",    f"£{total_cost:,.0f}")
k5.metric("Bidfood Spend",      f"£{total_ordered:,.0f}")

st.divider()

# ── Main tabs ────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["📊 Overview", "🏪 Sites", "📦 SKUs", "🍽 Recipe", "📋 Run Log"]
)

# ── TAB 1: Overview ──────────────────────────────────────────────────────────────
with tab1:
    c1, c2 = st.columns([1, 2])
    with c1:
        st.subheader("Compliance Split")
        n_sur = int((site_summ["Surplus"] > 0).sum()) if not site_summ.empty else 0
        n_def = int((site_summ["Deficit"] > 0).sum()) if not site_summ.empty else 0
        n_ex  = int(((site_summ["Deficit"] == 0) & (site_summ["Surplus"] > 0)).sum()) if not site_summ.empty else 0
        fig = go.Figure(go.Pie(
            labels=["Surplus","Deficit","Exact"],
            values=[n_sur, n_def, n_ex],
            marker_colors=["#538135","#C00000","#2E75B6"],
            hole=0.55, textinfo="label+percent",
        ))
        fig.update_layout(height=260, margin=dict(t=0,b=0,l=0,r=0), showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.subheader("Top 10 Deficit SKUs (all sites)")
        if not compliance.empty and "Status" in compliance.columns:
            def_df = (
                compliance[compliance["Status"] == "Deficit"]
                .groupby(["SKU","Ingredient"])
                .agg(Total_Gap=("Gap","sum"))
                .reset_index()
                .sort_values("Total_Gap")
                .head(10)
            )
            if not def_df.empty:
                fig2 = px.bar(def_df, x="Total_Gap", y="Ingredient", orientation="h",
                              color_discrete_sequence=["#C00000"])
                fig2.update_layout(height=260, margin=dict(t=0,b=0,l=0,r=10))
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.success("No deficits this week!")

    st.subheader("Site Compliance Summary")
    if not site_summ.empty:
        def _pct_color(val):
            try:
                pct = float(str(val).replace("%",""))
                if pct >= 80:   return "color: #266F00; font-weight:700"
                elif pct >= 50: return "color: #B8860B; font-weight:700"
                else:           return "color: #C00000; font-weight:700"
            except:
                return ""
        disp = site_summ.copy()
        if "Compliance_%" in disp.columns:
            disp["Compliance_%"] = disp["Compliance_%"].map(lambda x: f"{float(x):.1f}%")
        styled = disp.style.applymap(_pct_color, subset=["Compliance_%"] if "Compliance_%" in disp.columns else [])
        st.dataframe(styled, use_container_width=True, height=340)

# ── TAB 2: Sites ─────────────────────────────────────────────────────────────────
with tab2:
    st.subheader("Site-level Compliance Detail")
    if not compliance.empty:
        f1, f2, f3 = st.columns(3)
        with f1:
            sites = ["All"] + sorted(compliance["Site_Key"].dropna().unique().tolist())
            sel   = st.selectbox("Site", sites)
        with f2:
            sel_st = st.selectbox("Status", ["All","Surplus","Deficit","Exact"])
        with f3:
            skus   = ["All"] + sorted(compliance["SKU"].dropna().unique().tolist())
            sel_sk = st.selectbox("SKU", skus)

        disp = compliance.copy()
        if sel    != "All": disp = disp[disp["Site_Key"] == sel]
        if sel_st != "All": disp = disp[disp["Status"]   == sel_st]
        if sel_sk != "All": disp = disp[disp["SKU"]      == sel_sk]

        def _status_bg(val):
            c = {"Surplus":"#E5F5E0","Deficit":"#FFE8E8","Exact":"#E8F0FF"}
            return f"background-color: {c.get(val,'')}"

        st.dataframe(
            disp.style.applymap(_status_bg, subset=["Status"] if "Status" in disp.columns else []),
            use_container_width=True, height=500
        )
        st.caption(f"{len(disp):,} rows")

# ── TAB 3: SKUs ──────────────────────────────────────────────────────────────────
with tab3:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Ingredient Requirements (System B)")
        if not ingredient_s.empty:
            top = ingredient_s.copy()
            if "Total_Raw_Qty" in top.columns:
                top = top.sort_values("Total_Raw_Qty", ascending=False)
            st.dataframe(top.head(30), use_container_width=True, height=380)
    with c2:
        st.subheader("Bidfood Stock Ordered (System A)")
        if not bidfood_s.empty:
            top2 = bidfood_s.copy()
            if "Total_Ordered_Qty" in top2.columns:
                top2["Total_Ordered_Qty"] = pd.to_numeric(top2["Total_Ordered_Qty"], errors="coerce")
                top2 = top2.sort_values("Total_Ordered_Qty", ascending=False)
            st.dataframe(top2.head(30), use_container_width=True, height=380)

# ── TAB 4: Recipe ────────────────────────────────────────────────────────────────
with tab4:
    st.subheader("Recipe Matching Summary")
    if not recipe_s.empty:
        st.dataframe(recipe_s, use_container_width=True, height=350)
    if not unmatched_s.empty:
        with st.expander(f"⚠️ Unmatched / No-recipe items ({len(unmatched_s):,} rows)"):
            st.dataframe(unmatched_s, use_container_width=True, height=250)

# ── TAB 5: Run Log ────────────────────────────────────────────────────────────────
with tab5:
    st.subheader("Pipeline Run History")
    if not run_log.empty:
        st.dataframe(run_log.sort_index(ascending=False), use_container_width=True, height=400)
    else:
        st.info("No run history yet.")
    st.markdown("---")
    st.markdown(
        "**Pipeline runs every Monday at 8am** via Google Cloud Scheduler.  \n"
        "To trigger manually: Google Cloud Console → Cloud Run → Jobs → **lkn-pipeline** → Run Job."
    )
