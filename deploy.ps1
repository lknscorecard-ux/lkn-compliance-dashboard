# deploy.ps1  —  LKN Cloud Compliance One-Command Deploy (Windows PowerShell)
# Run from the Compliance folder:
#   powershell -ExecutionPolicy Bypass -File deploy.ps1

$ErrorActionPreference = "Continue"

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
$PROJECT_ID  = "compliance-501910"
$REGION      = "europe-west2"
$FOLDER_ID   = "1BV1zLMulHiQXWdXucv6-4JEOkF19eypP"
$RESULTS_SID = "1VafKGoN8tNMHRDfMyfBJwKCMVtLOt5yoZB6mLTY3Aw8"
$MAPPING_SID = "1P94V4FDrx9TFmDhUuPWIP2EGz4Dp2gCmjJfTkoebTUw"
$SA_NAME     = "lkn-pipeline-sa"
$SA_EMAIL    = "$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
$JOB_NAME    = "lkn-pipeline"
$REPO_NAME   = "lkn"
# ──────────────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "========================================================"
Write-Host "   LKN Cloud Compliance  --  One-Command Deploy"
Write-Host "========================================================"
Write-Host "Project : $PROJECT_ID"
Write-Host "Region  : $REGION"
Write-Host ""

# 1. Set project
Write-Host ">> Setting project ..."
gcloud config set project $PROJECT_ID

# 2. Enable APIs
Write-Host ">> Enabling Google APIs ..."
gcloud services enable `
  drive.googleapis.com `
  sheets.googleapis.com `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  cloudscheduler.googleapis.com `
  secretmanager.googleapis.com `
  artifactregistry.googleapis.com `
  --quiet

# 3. Artifact Registry
Write-Host ">> Creating Artifact Registry repo ..."
gcloud artifacts repositories create $REPO_NAME `
  --repository-format=docker `
  --location=$REGION `
  --quiet 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "  (already exists)" }

# 4. Service account
Write-Host ">> Creating service account ..."
gcloud iam service-accounts create $SA_NAME `
  --display-name="LKN Pipeline" --quiet 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "  (already exists)" }

# 5. IAM permissions
Write-Host ">> Granting IAM permissions ..."

gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:$SA_EMAIL" `
  --role="roles/run.invoker" --quiet

gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:$SA_EMAIL" `
  --role="roles/secretmanager.secretAccessor" --quiet

$PROJECT_NUMBER = gcloud projects describe $PROJECT_ID --format="value(projectNumber)"
$CB_SA = "$PROJECT_NUMBER@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:$CB_SA" `
  --role="roles/run.admin" --quiet

gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL `
  --member="serviceAccount:$CB_SA" `
  --role="roles/iam.serviceAccountUser" --quiet

Write-Host "  Remember to share Drive + Sheets with: $SA_EMAIL (Editor)"

# 6. Service account key -> Secret Manager
Write-Host ">> Creating service account key ..."
$KEY_PATH = "$env:TEMP\lkn-sa-key.json"
gcloud iam service-accounts keys create $KEY_PATH --iam-account=$SA_EMAIL --quiet

gcloud secrets create lkn-sa-key --data-file=$KEY_PATH --quiet 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud secrets versions add lkn-sa-key --data-file=$KEY_PATH
}
Remove-Item $KEY_PATH
Write-Host "  Key stored in Secret Manager as 'lkn-sa-key'"

# 7. Build and deploy via Cloud Build
Write-Host ""
Write-Host ">> Building Docker image and deploying Cloud Run job ..."
Write-Host "   (this takes 3-5 minutes)"
gcloud builds submit `
  --config cloudbuild.yaml `
  "--substitutions=_FOLDER_ID=$FOLDER_ID,_RESULTS_SID=$RESULTS_SID,_MAPPING_SID=$MAPPING_SID" `
  .

# 8. Cloud Scheduler
Write-Host ">> Setting up Cloud Scheduler (every Monday 8:00am) ..."
$SCHEDULER_URI = "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/$JOB_NAME`:run"
gcloud scheduler jobs create http "lkn-weekly-trigger" `
  --location=$REGION `
  --schedule="0 8 * * 1" `
  --time-zone="Europe/London" `
  --uri=$SCHEDULER_URI `
  --message-body='{}' `
  --oauth-service-account-email=$SA_EMAIL `
  --quiet 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "  (scheduler job already exists)" }

Write-Host ""
Write-Host "========================================================"
Write-Host "   Setup Complete!"
Write-Host "========================================================"
Write-Host ""
Write-Host "Service account : $SA_EMAIL"
Write-Host "Cloud Run job   : $JOB_NAME  ($REGION)"
Write-Host "Schedule        : Every Monday 8:00am (Europe/London)"
Write-Host "Results Sheet   : https://docs.google.com/spreadsheets/d/$RESULTS_SID"
Write-Host ""
Write-Host "NEXT STEPS:"
Write-Host "  1. Share the Drive folder with $SA_EMAIL (Editor)"
Write-Host "  2. Share the Compliance Results sheet with $SA_EMAIL (Editor)"
Write-Host "  3. Share the Drop Account Mapping sheet with $SA_EMAIL (Editor)"
Write-Host "  4. Drop weekly files into Drive and test:"
Write-Host "     gcloud run jobs execute $JOB_NAME --region=$REGION"
Write-Host ""
