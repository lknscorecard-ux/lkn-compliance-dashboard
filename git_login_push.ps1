cd "C:\Users\TEJAS\Desktop\Compliance"

# Step 1: Set your GitHub username
$username = Read-Host "Enter your GitHub username"

# Step 2: Enter your Personal Access Token (PAT)
# Get one from: GitHub → Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
# Required scope: repo
$pat = Read-Host "Enter your GitHub Personal Access Token" -AsSecureString
$patPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pat)
)

# Step 3: Set remote URL with credentials embedded
git remote set-url origin "https://$username`:$patPlain@github.com/lknscorecard-ux/lkn-compliance-dashboard.git"

# Step 4: Push
Write-Host "`nPushing to GitHub..." -ForegroundColor Cyan
git push

if ($LASTEXITCODE -eq 0) {
    Write-Host "`nSUCCESS - changes pushed." -ForegroundColor Green
} else {
    Write-Host "`nFailed. Check your username/token and try again." -ForegroundColor Red
}

# Step 5: Remove credentials from remote URL (security — don't leave PAT in git config)
git remote set-url origin "https://github.com/lknscorecard-ux/lkn-compliance-dashboard.git"

Write-Host "`nDone. Press any key to close..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
