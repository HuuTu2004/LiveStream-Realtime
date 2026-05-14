<#
.SYNOPSIS
  Deploy LiveTalking lên Vast.ai instance từ máy Windows local.

.DESCRIPTION
  Quy trình:
    1. Test SSH connection
    2. Git clone/pull repo trên instance
    3. SCP wav2lip.pth + avatar data (heavy, user-specific)
    4. Chạy scripts/vastai/setup.sh trên instance (venv + torch + deps)
    5. In hướng dẫn start server

  Code chính lấy qua git clone — KHÔNG scp toàn bộ source.
  Trước khi chạy, commit + push mọi thay đổi local lên main.

.PARAMETER InstanceHost
  Public IP hoặc hostname của instance (lấy từ vastai "Direct ssh connect").

.PARAMETER Port
  SSH port mapped bởi Vast (ko phải 22).

.PARAMETER KeyPath
  Đường dẫn SSH private key đã add vào Vast.

.PARAMETER AvatarId
  Avatar dir trong data/avatars/ để upload. Default: wav2lip256_avatar1.

.PARAMETER SkipAssets
  Bỏ qua scp wav2lip.pth + avatar (đã upload trước).

.PARAMETER SkipSetup
  Bỏ qua bước chạy setup.sh (đã setup trước).

.EXAMPLE
  .\scripts\vastai\deploy_from_windows.ps1 -InstanceHost 171.226.34.64 -Port 56020 -KeyPath $HOME\.ssh\vast_key

.EXAMPLE
  # Chỉ re-sync code (đã setup trước):
  .\scripts\vastai\deploy_from_windows.ps1 -InstanceHost 171.226.34.64 -Port 56020 -KeyPath $HOME\.ssh\vast_key -SkipAssets -SkipSetup
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstanceHost,
  [Parameter(Mandatory = $true)][int]$Port,
  [Parameter(Mandatory = $true)][string]$KeyPath,
  [string]$User = 'root',
  [string]$AvatarId = 'wav2lip256_avatar1',
  [string]$RemoteDir = '/workspace/LiveTalking',
  [switch]$SkipAssets,
  [switch]$SkipSetup
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
# Tìm LiveTalking/ root: nếu script được chạy từ subdir, đi lên đến khi thấy app.py
$LiveTalking = $PSScriptRoot
while ($LiveTalking -and -not (Test-Path (Join-Path $LiveTalking 'app.py'))) {
  $LiveTalking = Split-Path -Parent $LiveTalking
}
if (-not $LiveTalking) {
  throw "Không tìm thấy LiveTalking root (app.py). Chạy script từ trong repo."
}
Write-Host "[deploy] Repo root: $LiveTalking" -ForegroundColor Cyan

$SshArgs = @('-i', $KeyPath, '-o', 'StrictHostKeyChecking=accept-new', '-p', $Port, "$User@$InstanceHost")
$ScpArgs = @('-i', $KeyPath, '-o', 'StrictHostKeyChecking=accept-new', '-P', $Port)

function Invoke-Ssh([string]$Cmd) {
  Write-Host "[ssh] $Cmd" -ForegroundColor DarkGray
  & ssh @SshArgs $Cmd
  if ($LASTEXITCODE -ne 0) { throw "ssh failed: exit $LASTEXITCODE" }
}

function Send-Scp([string]$LocalPath, [string]$RemotePath) {
  if (-not (Test-Path $LocalPath)) {
    Write-Warning "[scp] skip: $LocalPath không tồn tại"
    return
  }
  Write-Host "[scp] $LocalPath → ${User}@${InstanceHost}:${RemotePath}" -ForegroundColor Cyan
  if ((Get-Item $LocalPath).PSIsContainer) {
    & scp @ScpArgs -r $LocalPath "${User}@${InstanceHost}:${RemotePath}"
  } else {
    & scp @ScpArgs $LocalPath "${User}@${InstanceHost}:${RemotePath}"
  }
  if ($LASTEXITCODE -ne 0) { throw "scp failed: exit $LASTEXITCODE" }
}

# ─── 1. Test SSH ───────────────────────────────────────────────────────────
Write-Host "`n[1/4] Test SSH connection..." -ForegroundColor Yellow
Invoke-Ssh "echo CONNECTED; uname -a; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

# ─── 2. Git clone/pull repo ────────────────────────────────────────────────
Write-Host "`n[2/4] Git clone/pull repo trên instance..." -ForegroundColor Yellow
$RemoteRepo = 'https://github.com/HuuTu2004/LiveStream-Realtime.git'
Invoke-Ssh @"
set -e
mkdir -p /workspace
cd /workspace
if [ ! -d $RemoteDir/.git ]; then
  git clone --depth 1 $RemoteRepo $RemoteDir
else
  cd $RemoteDir && git pull --rebase --autostash
fi
cd $RemoteDir && git log -1 --oneline
mkdir -p models data/avatars
"@

# ─── 3. SCP heavy assets ───────────────────────────────────────────────────
if (-not $SkipAssets) {
  Write-Host "`n[3/4] SCP wav2lip.pth + avatar data..." -ForegroundColor Yellow

  $Wav2Lip = Join-Path $LiveTalking 'models\wav2lip.pth'
  if (Test-Path $Wav2Lip) {
    # Check remote size — skip nếu đã có và cùng size
    $LocalSize = (Get-Item $Wav2Lip).Length
    $RemoteSize = (& ssh @SshArgs "stat -c %s $RemoteDir/models/wav2lip.pth 2>/dev/null || echo 0").Trim()
    if ($RemoteSize -eq "$LocalSize") {
      Write-Host "[scp] skip wav2lip.pth (same size $LocalSize bytes)" -ForegroundColor DarkGreen
    } else {
      Send-Scp $Wav2Lip "$RemoteDir/models/wav2lip.pth"
    }
  } else {
    Write-Warning "models\wav2lip.pth không tồn tại — server sẽ tự pull từ HF mirror."
  }

  $AvatarDir = Join-Path $LiveTalking "data\avatars\$AvatarId"
  if (Test-Path $AvatarDir) {
    Invoke-Ssh "mkdir -p $RemoteDir/data/avatars"
    # Check remote dir exists + has data
    $RemoteCount = (& ssh @SshArgs "find $RemoteDir/data/avatars/$AvatarId -type f 2>/dev/null | wc -l").Trim()
    if ([int]$RemoteCount -gt 0) {
      Write-Host "[scp] avatar $AvatarId đã tồn tại remote ($RemoteCount files) — skip" -ForegroundColor DarkGreen
    } else {
      Send-Scp $AvatarDir "$RemoteDir/data/avatars/"
    }
  } else {
    Write-Warning "Avatar dir $AvatarDir không tồn tại."
  }
} else {
  Write-Host "`n[3/4] Skip assets (--SkipAssets)" -ForegroundColor DarkYellow
}

# ─── 4. Run setup.sh ────────────────────────────────────────────────────────
if (-not $SkipSetup) {
  Write-Host "`n[4/4] Chạy setup.sh trên instance (mất ~5-10 phút)..." -ForegroundColor Yellow
  Invoke-Ssh "cd $RemoteDir && bash scripts/vastai/setup.sh"
} else {
  Write-Host "`n[4/4] Skip setup (--SkipSetup)" -ForegroundColor DarkYellow
}

# ─── Done ──────────────────────────────────────────────────────────────────
Write-Host "`n═══════════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host " Deploy OK. Start server:" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host @"

  ssh -i $KeyPath -p $Port $User@$InstanceHost
  cd $RemoteDir
  bash scripts/vastai/start.sh

  Web admin: http://<PUBLIC_IPADDR mapped to 8010>:8010/

  Nhớ expose port 8010 (TCP) khi tạo instance Vast.
  Nếu vieneu_mode=gpu fail (Blackwell), fallback:
    VIENEU_MODE=turbo bash scripts/vastai/start.sh
"@ -ForegroundColor White
