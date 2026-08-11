#!/usr/bin/env bash
# deploy.sh  —  One-command setup for LKN Cloud Compliance System
# ================================================================
# Run this once from your Compliance folder:
#   bash deploy.sh
#
# Prerequisites:
#   - gcloud CLI installed and authenticated:  gcloud auth login
#   - Docker Desktop running (for building the image)
#   - A Google Cloud project created

set -euo pipefail

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
PROJECT_ID="compliance-501910"
REGION="europe-west2"
NOTIFICATION_EMAIL="rohitbirajdar518@gmail.com"

FOLDER_ID="1BV1zLMulHiQXWdXucv6-4JEOkF19eypP"
RESULTS_SID="1VafKGoN8tNMHRDfMyfBJwKCMVtLOt5yoZB6mLTY3Aw8"
MAPPING_SID="1P94V4FDrx9TFmDhUuPWIP2EGz4Dp2gCmjJfTkoebTUw"
# ──────────────────────────────────────────────────────────────────────────────

SA_NAME="lkn-pipeline-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
JOB_NAME="lkn-pipeline"
REPO_NAME="lkn"
SCHEDULER_JOB="lkn-weekly-trigger"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║       LKN Cloud Compliance  —  One-Command Deploy       ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Project:  $PROJECT_ID"
echo "Region:   $REGION"
echo ""

# 1. Set project
gcloud config set project "$PROJECT_ID"

# 2. Enable required APIs
echo "► Enabling Google APIs ..."
gcloud services enable \
  drive.googleapis.com \
  sheets.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --quiet

# 3. Create Artifact Registry repo
echo "► Creating Artifact Registry ..."
gcloud artifacts repositories create "$REPO_NAME" \
  --repository-format=docker \
  --location="$REGION" \
  --quiet 2>/dev/null || echo "  (already exists)"

# 4. Create service account
echo "► Creating service account ..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="LKN Pipeline" --quiet 2>/dev/null || echo "  (already exists)"

# 5. Grant permissions
echo "► Granting permissions ..."

# Pipeline SA: allow Cloud Scheduler to invoke Cloud Run jobs as this SA
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.invoker" --quiet 2>/dev/null || true

# Pipeline SA: allow reading secrets from Secret Manager at runtime
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" --quiet 2>/dev/null || true

# Cloud Build SA: needs to deploy Cloud Run jobs and act as the pipeline SA
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$CB_SA" \
  --role="roles/run.admin" --quiet 2>/dev/null || true

gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:$CB_SA" \
  --role="roles/iam.serviceAccountUser" --quiet 2>/dev/null || true

echo "  Note: Drive + Sheets access is granted by sharing those resources with:"
echo "        $SA_EMAIL"

# 6. Create and store service account key in Secret Manager
echo "► Creating service account key ..."
gcloud iam service-accounts keys create /tmp/lkn-sa-key.json \
  --iam-account="$SA_EMAIL" --quiet
gcloud secrets create lkn-sa-key --data-file=/tmp/lkn-sa-key.json --quiet 2>/dev/null || \
  gcloud secrets versions add lkn-sa-key --data-file=/tmp/lkn-sa-key.json
rm /tmp/lkn-sa-key.json
echo "  Key stored in Secret Manager as 'lkn-sa-key'"

# 7 & 8. Google Drive + Sheets IDs (pre-configured above)
echo "► Using pre-configured Google Drive and Sheets IDs ..."
echo "  Drive folder:   $FOLDER_ID"
echo "  Results sheet:  $RESULTS_SID"
echo "  Mapping sheet:  $MAPPING_SID"
echo ""
echo "  ⚠  Make sure these resources are shared with:  $SA_EMAIL  (Editor)"

# 9. Build and deploy
echo ""
echo "► Building and deploying pipeline to Cloud Run ..."
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions="_FOLDER_ID=${FOLDER_ID},_RESULTS_SID=${RESULTS_SID},_MAPPING_SID=${MAPPING_SID}" \
  .

# 10. Set up Cloud Scheduler (every Monday 8am London time)
echo "► Setting up Cloud Scheduler (every Monday 8:00am) ..."
gcloud scheduler jobs create http "$SCHEDULER_JOB" \
  --location="$REGION" \
  --schedule="0 8 * * 1" \
  --time-zone="Europe/London" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
  --message-body='{}' \
  --oauth-service-account-email="$SA_EMAIL" \
  --quiet 2>/dev/null || echo "  (scheduler job already exists)"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║                    ✓  Setup Complete                    ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Pipeline:  Cloud Run Job '$JOB_NAME'  in $REGION"
echo "Schedule:  Every Monday 8:00am (Europe/London)"
echo "Results:   Google Sheet https://docs.google.com/spreadsheets/d/$RESULTS_SID"
echo ""
echo "Next steps:"
echo "  1. Drop your 3 weekly files into the Google Drive folder"
echo "  2. Wait for Monday 8am OR manually trigger:"
echo "     gcloud run jobs execute $JOB_NAME --region=$REGION"
echo ""
echo "  3. Deploy the dashboard:"
echo "     → Push to GitHub, connect at https://share.streamlit.io"
echo "     → Set secrets: GOOGLE_CREDENTIALS_JSON and RESULTS_SHEET_ID=$RESULTS_SID"
echo ""
