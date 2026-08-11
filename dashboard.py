"""
dashboard.py — LKN Compliance Dashboard
========================================
Run with:  streamlit run dashboard.py

Requires:
  pip install streamlit pandas plotly openpyxl xlsxwriter python-calamine requests
  (optional for private Google Sheets)  pip install gspread google-auth
"""

import os, sys, io, warnings
warnings.filterwarnings("ignore")

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Engine imports (same folder)
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

import engine_recipe
import engine_ingredient
import engine_bidfood
import engine_opalion
import engine_compliance
import gsheet_client
from config import PLU_FILE, RB_FILE, GSHEET_URL, GSHEET_WORKSHEET, GSHEET_CREDS_FILE

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LKN Compliance Dashboard",
    page_icon="🍗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  [data-testid="stMetricValue"] { font-size: 2rem; font-weight: 700; }
  .surplus  { color: #266F00; font-weight: 700; }
  .deficit  { color: #C00000; font-weight: 700; }
  .exact    { color: #2E75B6; font-weight: 700; }
  .stAlert  { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://via.placeholder.com/180x50/1F3864/FFFFFF?text=🍗+LKN+Compliance",
             use_column_width=True)
    st.markdown("---")
    st.header("📁 Upload Weekly Files")

    items_file    = st.file_uploader("Items-wise Report (.xlsx)",   type=["xlsx"], key="items")
    options_file  = st.file_uploader("Options-wise Report (.xlsx)", type=["xlsx"], key="options")
    bidfood_file  = st.file_uploader("Bidfood Orders (.xlsx)",      type=["xlsx"], key="bidfood")
    opalion_file  = st.file_uploader("Opalion Packaging (.csv) — optional",
                                      type=["csv"], key="opalion")
    if opalion_file:
        st.caption("📦 Packaging compliance will be included")

    st.markdown("---")
    st.header("🔗 Live Mapping (Google Sheet)")
    gsheet_url = st.text_input(
        "Drop Account Mapping URL",
        value=GSHEET_URL,
        placeholder="https://docs.google.com/spreadsheets/d/...",
        help=(
            "Share your sheet as 'Anyone with link can view', "
            "then paste the URL here.\n\n"
            "Or place 'Bidfood Drop Account Mapping.xlsx' in the Compliance folder "
            "and leave this blank (offline fallback)."
        ),
    )

    local_fallback = os.path.join(DIR, "Bidfood Drop Account Mapping.xlsx")

    st.markdown("---")
    run_btn = st.button("▶  Run Analysis", type="primary", use_container_width=True)

    if st.button("🗑  Clear Results", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

# ── Static file checks ────────────────────────────────────────────────────────
missing_static = []
if not os.path.exists(PLU_FILE): missing_static.append("PLU_Mapping_Complete.xlsx")
if not os.path.exists(RB_FILE):  missing_static.append("Recipe builder.xlsx")

# ── Welcome screen ────────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.title("🍗 LKN Compliance Dashboard")
    st.markdown("""
    **How to use:**
    1. Upload the three weekly files in the sidebar
    2. Paste your Google Sheet URL (Drop Account Mapping)
    3. Click **▶ Run Analysis**

    The system will run all three engines automatically and show the compliance dashboard.
    """)

    if missing_static:
        st.error(
            f"⚠️ Missing static reference files: {', '.join(missing_static)}\n\n"
            f"Place them in: `{DIR}`"
        )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.info("**System A**\nBidfood stock ordered\nper site per SKU")
    with col2:
        st.info("**System B**\nIngredient requirements\nfrom Urban Piper sales")
    with col3:
        st.success("**System C**\nCompliance gap\nA − B = Surplus/Deficit")
    with col4:
        st.info("**Packaging**\nOpalion orders vs\nrecipe packaging needs")
    st.stop()

# ── Run pipeline ──────────────────────────────────────────────────────────────
if run_btn:
    errors = []
    if not items_file:   errors.append("Items-wise Report")
    if not options_file: errors.append("Options-wise Report")
    if not bidfood_file: errors.append("Bidfood Orders")
    if missing_static:   errors.append(f"Static files: {', '.join(missing_static)}")

    if errors:
        st.sidebar.error("Missing:\n" + "\n".join(f"• {e}" for e in errors))
        st.stop()

    with st.sidebar:
        prog = st.progress(0, "Starting …")

    try:
        # -- Load uploaded files -----------------------------------------------
        prog.progress(5, "Loading uploaded files …")
        items_df   = pd.read_excel(items_file,   dtype=str, engine="openpyxl")
        options_df = pd.read_excel(options_file, dtype=str, engine="openpyxl")
        bf_df      = pd.read_excel(bidfood_file, dtype=str, engine="openpyxl")
        items_df.columns   = items_df.columns.str.strip()
        options_df.columns = options_df.columns.str.strip()
        bf_df.columns      = bf_df.columns.str.strip()

        # -- Load PLU mapping --------------------------------------------------
        prog.progress(10, "Loading PLU mapping …")
        plu_df = pd.read_excel(
            PLU_FILE, sheet_name="Master PLU Mapping", dtype=str, engine="calamine"
        ).fillna("").apply(lambda c: c.str.strip() if c.dtype == object else c)

        # -- Live Drop Account Mapping -----------------------------------------
        prog.progress(15, "Fetching Drop Account Mapping …")
        mapping_df, mapping_source = gsheet_client.read_drop_account_mapping(
            sheet_url=gsheet_url,
            worksheet=GSHEET_WORKSHEET,
            creds_file=GSHEET_CREDS_FILE,
            local_fallback=local_fallback,
        )

        # -- System B: Recipe matching -----------------------------------------
        prog.progress(25, "System B — matching recipes …")
        matched_df, summary_df, unmatched_items, unmatched_opts = engine_recipe.run(
            items_df, options_df, plu_df
        )

        # -- System B: Ingredient requirements ---------------------------------
        prog.progress(50, "System B — computing ingredient requirements …")
        site_raw, raw_summary, ingredient_summary, unmatched_report = engine_ingredient.run(
            matched_df, RB_FILE
        )

        # -- System A: Bidfood stock -------------------------------------------
        prog.progress(70, "System A — mapping Bidfood stock …")
        site_stock, sku_summary, bf_lkn, bf_unmatched = engine_bidfood.run(bf_df, mapping_df)

        # -- System C: Compliance gap ------------------------------------------
        prog.progress(80, "System C — computing compliance gap …")
        compliance = engine_compliance.run(site_raw, site_stock)
        site_summ  = engine_compliance.site_summary(compliance)

        # -- Packaging: Opalion (optional) ------------------------------------
        pkg_compliance    = pd.DataFrame()
        pkg_site_summ     = pd.DataFrame()
        pkg_sku_summary   = pd.DataFrame()
        opal_unmatched    = pd.DataFrame()
        opalion_fname     = None

        if opalion_file:
            prog.progress(88, "Packaging — processing Opalion report …")
            import io as _io
            opal_df = pd.read_csv(_io.BytesIO(opalion_file.read()))
            opal_df.columns = opal_df.columns.str.strip()
            site_packaging, pkg_sku_summary, opal_unmatched = engine_opalion.run(
                opal_df, mapping_df
            )
            pkg_compliance = engine_compliance.packaging_compliance(site_raw, site_packaging)
            pkg_site_summ  = engine_compliance.packaging_site_summary(pkg_compliance)
            opalion_fname  = opalion_file.name

        # -- Store in session state --------------------------------------------
        st.session_state["results"] = {
            "matched_df":          matched_df,
            "summary_df":          summary_df,
            "unmatched_items":     unmatched_items,
            "unmatched_opts":      unmatched_opts,
            "site_raw":            site_raw,
            "raw_summary":         raw_summary,
            "ingredient_summary":  ingredient_summary,
            "unmatched_report":    unmatched_report,
            "site_stock":          site_stock,
            "sku_summary":         sku_summary,
            "bf_lkn":              bf_lkn,
            "bf_unmatched":        bf_unmatched,
            "compliance":          compliance,
            "site_summ":           site_summ,
            "pkg_compliance":      pkg_compliance,
            "pkg_site_summ":       pkg_site_summ,
            "pkg_sku_summary":     pkg_sku_summary,
            "opal_unmatched":      opal_unmatched,
            "mapping_source":      mapping_source,
            "items_fname":         items_file.name,
            "options_fname":       options_file.name,
            "bidfood_fname":       bidfood_file.name,
            "opalion_fname":       opalion_fname,
        }
        prog.progress(100, "Done ✓")

    except Exception as e:
        st.sidebar.error(f"Pipeline error:\n{e}")
        import traceback
        st.exception(e)
        st.stop()

# ── Dashboard ─────────────────────────────────────────────────────────────────
r = st.session_state["results"]
compliance         = r["compliance"]
site_summ          = r["site_summ"]
ingredient_summary = r["ingredient_summary"]
site_stock         = r["site_stock"]
site_raw           = r["site_raw"]
sku_summary        = r["sku_summary"]
unmatched_report   = r["unmatched_report"]
bf_unmatched       = r["bf_unmatched"]
pkg_compliance     = r.get("pkg_compliance",   pd.DataFrame())
pkg_site_summ      = r.get("pkg_site_summ",    pd.DataFrame())
pkg_sku_summary    = r.get("pkg_sku_summary",  pd.DataFrame())
opal_unmatched     = r.get("opal_unmatched",   pd.DataFrame())
has_packaging      = not pkg_compliance.empty

# Title bar
col_t1, col_t2 = st.columns([3,1])
with col_t1:
    st.title("🍗 LKN Compliance Dashboard")
with col_t2:
    src = r["mapping_source"]
    dot = "🟢" if "live" in src.lower() or "service" in src.lower() else "🟡"
    st.markdown(f"**Mapping:** {dot} {src}")

# Alert for unmatched accounts
if len(bf_unmatched) > 0:
    n_acc = bf_unmatched["Customer Code"].nunique() if "Customer Code" in bf_unmatched.columns else "?"
    st.warning(
        f"⚠️ **{n_acc} Bidfood accounts not in Drop Account Mapping** — "
        f"update the Google Sheet to include them."
    )

# ── KPI Row ───────────────────────────────────────────────────────────────────
total_sites    = site_summ.shape[0]
deficit_sites  = (site_summ["Deficit"] > 0).sum()
surplus_sites  = (site_summ["Surplus"] > 0).sum()
total_cost     = site_raw["Total_Cost"].sum() if "Total_Cost" in site_raw.columns else 0
bidfood_spend  = site_stock["Total_Spend_GBP"].sum() if "Total_Spend_GBP" in site_stock.columns else 0
avg_compliance = site_summ["Compliance_%"].mean() if len(site_summ) else 0

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Sites Monitored",   total_sites)
k2.metric("Avg Compliance",    f"{avg_compliance:.1f}%",
          help="% of SKUs with Surplus or Exact match across all sites")
k3.metric("Sites with Deficit",deficit_sites,
          delta=f"{deficit_sites} need attention", delta_color="inverse")
k4.metric("Ingredient Cost",   f"£{total_cost:,.0f}",
          help="Total estimated ingredient cost from Urban Piper sales × recipe costs")
k5.metric("Bidfood Spend",     f"£{bidfood_spend:,.0f}",
          help="Total spend on Bidfood orders for LKN sites")

st.markdown("---")

# ── Main tabs ─────────────────────────────────────────────────────────────────
_tab_labels = ["📊 Overview", "🏪 Sites", "📦 SKUs", "🍽 Recipe Usage", "📫 Packaging", "⬇ Export"]
tab_overview, tab_sites, tab_skus, tab_recipe, tab_packaging, tab_export = st.tabs(_tab_labels)

# ── TAB 1: Overview ───────────────────────────────────────────────────────────
with tab_overview:
    col_donut, col_top = st.columns([1, 2])

    with col_donut:
        st.subheader("Site Compliance Split")
        n_surplus = (site_summ["Surplus"] > 0).sum()
        n_exact   = ((site_summ["Exact"] > 0) & (site_summ["Deficit"] == 0)).sum()
        n_deficit = (site_summ["Deficit"] > 0).sum()
        fig_donut = go.Figure(go.Pie(
            labels=["Surplus","Exact","Deficit"],
            values=[n_surplus, n_exact, n_deficit],
            marker_colors=["#538135","#2E75B6","#C00000"],
            hole=0.55,
            textinfo="label+percent",
        ))
        fig_donut.update_layout(height=280, margin=dict(t=10,b=10,l=10,r=10),
                                showlegend=False)
        st.plotly_chart(fig_donut, use_container_width=True)

    with col_top:
        st.subheader("Top Deficit SKUs (all sites)")
        deficit_df = (
            compliance[compliance["Status"] == "Deficit"]
            .groupby(["SKU","Ingredient"])
            .agg(Total_Gap=("Gap","sum"))
            .reset_index()
            .sort_values("Total_Gap")
            .head(10)
        )
        if not deficit_df.empty:
            fig_bar = px.bar(
                deficit_df,
                x="Total_Gap", y="Ingredient",
                orientation="h",
                color_discrete_sequence=["#C00000"],
                labels={"Total_Gap":"Gap (g or ml)","Ingredient":""},
            )
            fig_bar.update_layout(height=280, margin=dict(t=10,b=10,l=10,r=10))
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.success("No deficits found across all sites!")

    # Compliance summary table
    st.subheader("Site Compliance Summary")
    display_summ = site_summ.copy()
    display_summ["Compliance_%"] = display_summ["Compliance_%"].map(lambda x: f"{x:.1f}%")

    def _color_row(val):
        try:
            pct = float(str(val).replace("%",""))
            if pct >= 80:   return "color: #266F00"
            elif pct >= 50: return "color: #B8860B"
            else:           return "color: #C00000"
        except:
            return ""

    styled = display_summ.style.applymap(_color_row, subset=["Compliance_%"])
    st.dataframe(styled, use_container_width=True, height=350)

# ── TAB 2: Sites ─────────────────────────────────────────────────────────────
with tab_sites:
    st.subheader("Site-level Compliance Detail")

    # Filters
    f1, f2, f3 = st.columns(3)
    with f1:
        all_sites = sorted(compliance["Site_Key"].dropna().unique())
        sel_site  = st.selectbox("Filter by Site", ["All"] + list(all_sites))
    with f2:
        all_status = ["All","Surplus","Deficit","Exact"]
        sel_status = st.selectbox("Filter by Status", all_status)
    with f3:
        all_skus = sorted(compliance["SKU"].dropna().unique())
        sel_sku  = st.selectbox("Filter by SKU", ["All"] + list(all_skus))

    disp = compliance.copy()
    if sel_site   != "All": disp = disp[disp["Site_Key"] == sel_site]
    if sel_status != "All": disp = disp[disp["Status"]   == sel_status]
    if sel_sku    != "All": disp = disp[disp["SKU"]      == sel_sku]

    # Color Status column
    def _style_status(val):
        colors = {"Surplus":"#E5F5E0","Deficit":"#FFE8E8","Exact":"#E8F0FF"}
        return f"background-color: {colors.get(val,'')}"

    disp_styled = disp.style.applymap(_style_status, subset=["Status"])
    st.dataframe(disp_styled, use_container_width=True, height=500)
    st.caption(f"Showing {len(disp):,} rows")

# ── TAB 3: SKUs ──────────────────────────────────────────────────────────────
with tab_skus:
    st.subheader("SKU / Ingredient Overview")

    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Ingredient Requirements (System B)**")
        req_top = (
            ingredient_summary
            .sort_values("Total_Raw_Qty", ascending=False)
            .head(20)
        )
        st.dataframe(req_top, use_container_width=True, height=350)

    with c2:
        st.markdown("**Bidfood Stock Ordered (System A)**")
        sku_top = sku_summary.sort_values("Total_Ordered_Qty", ascending=False).head(20)
        st.dataframe(sku_top, use_container_width=True, height=350)

    st.subheader("Ingredient Requirements by Site")
    sites_b = sorted(site_raw["Store"].dropna().unique())
    sel_site_b = st.selectbox("Select Site", ["All"] + list(sites_b), key="sku_site")
    disp_b = site_raw if sel_site_b == "All" else site_raw[site_raw["Store"] == sel_site_b]
    st.dataframe(disp_b, use_container_width=True, height=300)

# ── TAB 4: Recipe Usage ───────────────────────────────────────────────────────
with tab_recipe:
    st.subheader("Recipe Matching Results (System B)")

    m1, m2, m3 = st.columns(3)
    m1.metric("Matched Rows",    f"{len(r['matched_df']):,}")
    m2.metric("Unmatched Items", f"{len(r['unmatched_items']):,}")
    m3.metric("Unmatched Options", f"{len(r['unmatched_opts']):,}")

    st.markdown("**Recipe Summary**")
    st.dataframe(r["summary_df"], use_container_width=True, height=300)

    if len(unmatched_report) > 0:
        with st.expander(f"⚠️ Unmatched / No-recipe items ({len(unmatched_report):,} rows)"):
            st.dataframe(unmatched_report, use_container_width=True, height=250)

# ── TAB 5: Packaging ─────────────────────────────────────────────────────────
with tab_packaging:
    if not has_packaging:
        st.info(
            "📂 Upload an **Opalion Packaging CSV** in the sidebar to see packaging compliance.\n\n"
            "The file is the 'Line Item Details by Company' export from the Opalion site."
        )
    else:
        st.subheader("Packaging Compliance — Opalion vs Recipe Requirements")

        # KPI row
        p1, p2, p3, p4 = st.columns(4)
        total_pkg_sites  = pkg_site_summ.shape[0] if not pkg_site_summ.empty else 0
        deficit_pkg      = int(pkg_site_summ["Deficit"].gt(0).sum()) if not pkg_site_summ.empty else 0
        avg_pkg_comp     = pkg_site_summ["Compliance_%"].mean() if not pkg_site_summ.empty else 0
        opalion_spend    = pkg_sku_summary["Total_Net_Sales"].sum() if not pkg_sku_summary.empty else 0
        p1.metric("Sites with Packaging Data",  total_pkg_sites)
        p2.metric("Avg Packaging Compliance",   f"{avg_pkg_comp:.1f}%")
        p3.metric("Sites with Deficit",         deficit_pkg,
                  delta=f"{deficit_pkg} need attention", delta_color="inverse")
        p4.metric("Opalion Spend (period)",     f"£{opalion_spend:,.0f}")

        st.markdown("---")
        col_donut2, col_top2 = st.columns([1, 2])

        with col_donut2:
            st.subheader("Site Packaging Split")
            n_ps = int(pkg_site_summ["Surplus"].gt(0).sum()) if not pkg_site_summ.empty else 0
            n_pe = int((pkg_site_summ["Exact"].gt(0) & pkg_site_summ["Deficit"].eq(0)).sum()) if not pkg_site_summ.empty else 0
            n_pd = int(pkg_site_summ["Deficit"].gt(0).sum()) if not pkg_site_summ.empty else 0
            fig_pkg = go.Figure(go.Pie(
                labels=["Surplus","Exact","Deficit"],
                values=[n_ps, n_pe, n_pd],
                marker_colors=["#538135","#2E75B6","#C00000"],
                hole=0.55,
                textinfo="label+percent",
            ))
            fig_pkg.update_layout(height=260, margin=dict(t=10,b=10,l=10,r=10),
                                  showlegend=False)
            st.plotly_chart(fig_pkg, use_container_width=True)

        with col_top2:
            st.subheader("Top Deficit Packaging Items")
            pkg_deficit = (
                pkg_compliance[pkg_compliance["Status"] == "Deficit"]
                .groupby("Ingredient")
                .agg(Total_Gap=("Gap","sum"))
                .reset_index()
                .sort_values("Total_Gap")
                .head(10)
            )
            if not pkg_deficit.empty:
                fig_pkg_bar = px.bar(
                    pkg_deficit,
                    x="Total_Gap", y="Ingredient",
                    orientation="h",
                    color_discrete_sequence=["#C00000"],
                    labels={"Total_Gap":"Gap (units)","Ingredient":""},
                )
                fig_pkg_bar.update_layout(height=260, margin=dict(t=10,b=10,l=10,r=10))
                st.plotly_chart(fig_pkg_bar, use_container_width=True)
            else:
                st.success("No packaging deficits found!")

        # Site packaging summary table
        st.subheader("Packaging Compliance by Site")
        if not pkg_site_summ.empty:
            disp_pkg_summ = pkg_site_summ.copy()
            disp_pkg_summ["Compliance_%"] = disp_pkg_summ["Compliance_%"].map(lambda x: f"{x:.1f}%")
            st.dataframe(disp_pkg_summ, use_container_width=True, height=300)

        # Detailed packaging compliance
        st.subheader("Packaging Detail")
        fp1, fp2 = st.columns(2)
        with fp1:
            all_pkg_sites = sorted(pkg_compliance["Site_Key"].dropna().unique())
            sel_pkg_site  = st.selectbox("Filter by Site", ["All"] + list(all_pkg_sites), key="pkg_site")
        with fp2:
            sel_pkg_status = st.selectbox("Filter by Status", ["All","Surplus","Deficit","Exact"], key="pkg_status")

        disp_pkg = pkg_compliance.copy()
        if sel_pkg_site   != "All": disp_pkg = disp_pkg[disp_pkg["Site_Key"] == sel_pkg_site]
        if sel_pkg_status != "All": disp_pkg = disp_pkg[disp_pkg["Status"]   == sel_pkg_status]

        def _style_pkg_status(val):
            colors = {"Surplus":"#E5F5E0","Deficit":"#FFE8E8","Exact":"#E8F0FF"}
            return f"background-color: {colors.get(val,'')}"

        st.dataframe(
            disp_pkg.style.applymap(_style_pkg_status, subset=["Status"]),
            use_container_width=True, height=400,
        )
        st.caption(f"Showing {len(disp_pkg):,} packaging rows")

        # Unmatched Opalion company names
        if not opal_unmatched.empty:
            with st.expander(f"⚠️ Unmatched Opalion companies ({len(opal_unmatched):,} rows) — add to Drop Account Mapping"):
                st.dataframe(
                    opal_unmatched[["Site_Label","Product_Name","Net_Qty","Total_Units"]].drop_duplicates(),
                    use_container_width=True, height=250,
                )

        # Opalion product summary
        with st.expander("📋 Opalion Product Summary (all sites)"):
            st.dataframe(pkg_sku_summary, use_container_width=True, height=300)


# ── TAB 6: Export ─────────────────────────────────────────────────────────────
with tab_export:
    st.subheader("Download Reports")

    def _to_excel_bytes(dfs_dict: dict) -> bytes:
        """Write multiple DataFrames to an Excel workbook in memory."""
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            for sheet, df in dfs_dict.items():
                df.to_excel(writer, sheet_name=sheet[:31], index=False)
        return buf.getvalue()

    col_d1, col_d2, col_d3, col_d4 = st.columns(4)

    with col_d1:
        st.markdown("**Compliance Report**")
        xlsx_c = _to_excel_bytes({
            "Compliance Gap":    compliance,
            "Site Summary":      site_summ,
        })
        st.download_button(
            "⬇ Compliance_Report.xlsx",
            data=xlsx_c,
            file_name="Compliance_Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_d2:
        st.markdown("**Ingredient Requirements**")
        xlsx_b = _to_excel_bytes({
            "Site Raw Material":    site_raw,
            "Raw Material Summary": r["raw_summary"],
            "SKU Summary":          ingredient_summary,
            "Unmatched":            unmatched_report,
        })
        st.download_button(
            "⬇ Ingredient_Requirements.xlsx",
            data=xlsx_b,
            file_name="Ingredient_Requirements.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_d3:
        st.markdown("**Bidfood Stock Report**")
        xlsx_a = _to_excel_bytes({
            "Site Stock":       site_stock,
            "SKU Summary":      sku_summary,
            "Unmatched Accounts": bf_unmatched,
        })
        st.download_button(
            "⬇ Bidfood_Stock.xlsx",
            data=xlsx_a,
            file_name="Bidfood_Stock.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_d4:
        st.markdown("**Packaging Report**")
        if has_packaging:
            xlsx_p = _to_excel_bytes({
                "Packaging Compliance": pkg_compliance,
                "Site Summary":         pkg_site_summ,
                "Product Summary":      pkg_sku_summary,
                "Unmatched Companies":  opal_unmatched,
            })
            st.download_button(
                "⬇ Packaging_Report.xlsx",
                data=xlsx_p,
                file_name="Packaging_Report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.caption("Upload Opalion CSV to enable")

    st.markdown("---")
    st.markdown("**Files used in this run:**")
    st.markdown(f"- Items:   `{r['items_fname']}`")
    st.markdown(f"- Options: `{r['options_fname']}`")
    st.markdown(f"- Bidfood: `{r['bidfood_fname']}`")
    if r.get("opalion_fname"):
        st.markdown(f"- Opalion: `{r['opalion_fname']}`")
    st.markdown(f"- Mapping: {r['mapping_source']}")
