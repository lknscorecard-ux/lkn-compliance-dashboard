"""
gsheet_client.py — Read Drop Account Mapping from Google Sheets (live) or local Excel.

Two modes (tried in order):
  1. Service account (private sheet) — if google_credentials.json exists in DIR
  2. Public CSV export — if the sheet is shared "Anyone with link can view"
  3. Local Excel fallback — Bidfood Drop Account Mapping.xlsx
"""

import os, re, io
import pandas as pd

_EXPECTED_COLS = [
    "Store Name",
    "Drop Account Number - Bidfood",
    "Site Key",
]


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("")
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip()
    return df


def read_drop_account_mapping(
    sheet_url: str,
    worksheet: str = "Site Mapping",
    creds_file: str = "",
    local_fallback: str = "",
) -> tuple[pd.DataFrame, str]:
    """
    Returns (DataFrame, source_label).
    Raises RuntimeError if all methods fail.
    """

    # ── Method 1: gspread service account ─────────────────────────────────────
    if creds_file and os.path.exists(creds_file):
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            creds  = Credentials.from_service_account_file(creds_file, scopes=scopes)
            gc     = gspread.authorize(creds)

            match = re.search(r"/d/([a-zA-Z0-9_-]+)", sheet_url or "")
            if not match:
                raise ValueError("Invalid Google Sheet URL")
            sh = gc.open_by_key(match.group(1))
            ws = sh.worksheet(worksheet)
            df = pd.DataFrame(ws.get_all_records()).pipe(_clean_df)
            return df, "Google Sheets (service account)"
        except Exception as e:
            pass  # fall through to next method

    # ── Method 2: Public CSV export ───────────────────────────────────────────
    if sheet_url and sheet_url.strip():
        try:
            import requests

            match = re.search(r"/d/([a-zA-Z0-9_-]+)", sheet_url)
            if match:
                sheet_id = match.group(1)
                url = (
                    f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                    f"/export?format=csv&sheet={worksheet}"
                )
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                df = pd.read_csv(io.StringIO(resp.text), dtype=str).pipe(_clean_df)
                return df, "Google Sheets (live CSV)"
        except Exception as e:
            pass  # fall through to fallback

    # ── Method 3: Local Excel fallback ────────────────────────────────────────
    if local_fallback and os.path.exists(local_fallback):
        df = pd.read_excel(
            local_fallback, sheet_name=worksheet, dtype=str, engine="calamine"
        ).pipe(_clean_df)
        return df, "Local Excel (offline fallback)"

    raise RuntimeError(
        "Could not load Drop Account Mapping.\n"
        "Either:\n"
        "  • Paste the Google Sheet URL and share the sheet as Anyone-with-link can view\n"
        "  • Or place google_credentials.json in the Compliance folder\n"
        "  • Or upload a local copy of Bidfood Drop Account Mapping.xlsx"
    )
