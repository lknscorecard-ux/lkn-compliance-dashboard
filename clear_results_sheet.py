"""
clear_results_sheet.py
======================
Clears compliance result tabs so you can start fresh with 3 clean weeks.

Run from the Compliance folder:
    python clear_results_sheet.py

Requires:
  - google_credentials.json in the same folder  (service account key file)
  - RESULTS_SHEET_ID set below (or as env var)
"""

import os, json
import gspread
from google.oauth2.service_account import Credentials

# ── Config ─────────────────────────────────────────────────────────────────────
RESULTS_SHEET_ID = os.environ.get("RESULTS_SHEET_ID", "")   # or paste ID directly here

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Tabs to fully clear (header row removed too — pipeline recreates them)
TABS_TO_CLEAR = [
    "Compliance Gap",
    "Site Summary",
    "Compliance History",
    "Site Summary History",
    "Bidfood Stock",
    "Bidfood Stock History",
]

# ── Auth ───────────────────────────────────────────────────────────────────────
creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
if creds_json:
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
else:
    creds_path = os.path.join(os.path.dirname(__file__), "google_credentials.json")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)

gc = gspread.authorize(creds)

if not RESULTS_SHEET_ID:
    raise ValueError("Set RESULTS_SHEET_ID in the script or as an environment variable.")

sh = gc.open_by_key(RESULTS_SHEET_ID)

# ── Clear each tab ─────────────────────────────────────────────────────────────
for tab_name in TABS_TO_CLEAR:
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
        print(f"  ✓ Cleared: {tab_name}")
    except gspread.exceptions.WorksheetNotFound:
        print(f"  – Skipped (not found): {tab_name}")

print("\nDone. All tabs cleared. Ready to run fresh pipeline runs.")
