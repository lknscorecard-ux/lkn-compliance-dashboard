import os

# Base directory — same folder as this file
DIR = os.path.dirname(os.path.abspath(__file__))

# ── Static reference files (already in the Compliance folder) ─────────────────
PLU_FILE = os.path.join(DIR, "PLU_Mapping_Complete.xlsx")
RB_FILE  = os.path.join(DIR, "Recipe builder.xlsx")

# ── Google Sheets — Drop Account Mapping ──────────────────────────────────────
# Set to the full Google Sheet URL.
# The sheet must be shared as "Anyone with the link can view".
# Example: https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs/edit
GSHEET_URL       = "https://docs.google.com/spreadsheets/d/1P94V4FDrx9TFmDhUuPWIP2EGz4Dp2gCmjJfTkoebTUw/edit"
GSHEET_WORKSHEET = "Site Mapping"

# If you prefer service-account auth (private sheet), place your credentials JSON
# file in the Compliance folder and set the path below:
GSHEET_CREDS_FILE = os.path.join(DIR, "google_credentials.json")

# ── Brands processed by Recipe Builder ────────────────────────────────────────
IN_SCOPE_BRANDS = {"Hot Chick", "WTF", "Korea Town", "Wing Fest", "Kuro Smash"}

BRAND_SHEETS = {
    "Hot Chick":  "Hot Chick",
    "WTF":        "WTF",
    "Korea Town": "Koreatown",
    "Wing Fest":  "Wing Fest",
    "Kuro Smash": "Kurosmash",
}

# ── Output paths ──────────────────────────────────────────────────────────────
OUT_DIR = DIR
