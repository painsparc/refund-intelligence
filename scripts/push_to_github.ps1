# scripts/push_to_github.ps1  -  commit everything safe and push to GitHub
#   PS X:\refund-intelligence> powershell -ExecutionPolicy Bypass -File scripts\push_to_github.ps1
#   optional:  -Message "my commit message"   -DryRun   (shows what would be committed, changes nothing)
param([string]$Message = "Real-data evaluation, labelling and download tooling, backend validation, decisions", [switch]$DryRun)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .git)) { throw "Not a git repository: $(Get-Location)" }
$remote = git remote get-url origin 2>$null
if (-not $remote) { throw "No 'origin' remote. Add one:  git remote add origin https://github.com/<you>/<repo>.git" }
$branch = git branch --show-current
Write-Host "Repository: $remote   branch: $branch"

# 1. stage everything the .gitignore allows
git add -A

# 2. safety checks on what is staged
$staged = git diff --cached --name-only
$bad = @()
foreach ($f in $staged) {
    if ($f -match '(^|/)data/raw/' -or $f -match '\.db$' -or $f -match '(^|/)\.venv/' -or $f -match '(^|/)\.env$' -or $f -match 'kaggle\.json') { $bad += $f; continue }
    if ((Test-Path $f) -and ((Get-Item $f).Length -gt 50MB)) { $bad += "$f (over 50 MB)" }
}
if ($bad.Count) {
    git reset -q
    Write-Host "STOPPED - these must not be committed:" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "  $_" }
    exit 1
}
Write-Host "`nFiles to be committed ($($staged.Count)):"
$staged | ForEach-Object { Write-Host "  $_" }

# data/labelling holds review TEXT from Amazon Reviews 2023 (no licence stated): fine in a private repo, think before making it public
if ($staged -match '^data/labelling/') { Write-Host "`nNote: data/labelling/ contains Amazon review text. Keep the repo private or remove it:  git reset data/labelling" -ForegroundColor Yellow }

if ($DryRun) { git reset -q; Write-Host "`nDry run - nothing committed."; exit 0 }
if (-not $staged) { Write-Host "Nothing to commit."; exit 0 }

# 3. commit + push
git commit -m $Message
git push -u origin $branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nPush failed. If GitHub says the remote has newer commits:  git pull --rebase origin $branch   then run this script again." -ForegroundColor Red
    exit 1
}
Write-Host "`nPushed to $remote ($branch)." -ForegroundColor Green
