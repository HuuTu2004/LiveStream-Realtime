# Chay app.py local Windows voi settings tach rieng - KHONG dung Vast.AI.
#
# Usage:
#   .\dev_local.ps1                 # port 8011, brain ON
#   .\dev_local.ps1 -Port 8010      # doi port
#   .\dev_local.ps1 -UiOnly         # serve static web/ thoi (khong backend)
#   .\dev_local.ps1 -NoBrain        # tat brain

param(
    [int]$Port = 8011,
    [switch]$UiOnly,
    [switch]$NoBrain,
    [string]$AvatarId = "wav2lip256_avatar1",
    [string]$Model = "wav2lip"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PyExe = ".\venv_talking\Scripts\python.exe"
if (-not (Test-Path $PyExe)) {
    Write-Error "Khong tim thay $PyExe - can venv_talking. Tao qua: python -m venv venv_talking"
    exit 1
}

# UI-only mode: serve static web/
if ($UiOnly) {
    Write-Host "[dev_local] UI-only mode - phuc vu web/ qua http.server" -ForegroundColor Cyan
    Write-Host "[dev_local] API calls se fail (khong co backend) - chi test layout/CSS." -ForegroundColor Yellow
    Write-Host "[dev_local] Mo: http://localhost:$Port" -ForegroundColor Green
    & $PyExe -m http.server $Port --directory web
    return
}

# Full backend voi settings.local.json
$env:PYTHONIOENCODING = "utf-8"
$env:LIVETALKING_SETTINGS_PATH = "data/settings.local.json"

if (-not (Test-Path "data/settings.local.json")) {
    Write-Warning "data/settings.local.json chua co - tao file mau truoc roi chay lai."
    exit 1
}

if ($NoBrain) {
    $brainFlag = "false"
} else {
    $brainFlag = "true"
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " LOCAL DEV - KHONG anh huong Vast.AI" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Settings:  $env:LIVETALKING_SETTINGS_PATH (gitignored)" -ForegroundColor Gray
Write-Host " Port:      $Port" -ForegroundColor Gray
Write-Host " Model:     $Model / $AvatarId" -ForegroundColor Gray
Write-Host " TTS:       vieneu (in-process, turbo mode)" -ForegroundColor Gray
Write-Host " Brain:     $brainFlag" -ForegroundColor Gray
Write-Host " URL:       http://localhost:$Port" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

& $PyExe -u app.py `
    --model $Model `
    --avatar_id $AvatarId `
    --tts vieneu `
    --vieneu_mode turbo `
    --brain_enabled $brainFlag `
    --listenport $Port
