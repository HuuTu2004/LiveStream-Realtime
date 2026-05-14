###############################################################################
#  Setup LiveTalking cho RTX 50x (Blackwell / sm_120) trên Windows
#
#  Yêu cầu trước:
#    - NVIDIA driver >= 570 (Blackwell-ready, có CUDA 12.8 runtime)
#    - Python 3.10 (venv_talking đã tạo sẵn ở repo root)
#    - ffmpeg trong PATH
#
#  Cách chạy:
#    powershell -ExecutionPolicy Bypass -File .\setup_5090.ps1
###############################################################################

$ErrorActionPreference = "Stop"

Write-Host "==> Kích hoạt venv_talking..." -ForegroundColor Cyan
$venvActivate = Join-Path $PSScriptRoot "venv_talking\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Error "Không tìm thấy venv_talking. Tạo trước: python -m venv venv_talking"
    exit 1
}
. $venvActivate

Write-Host "==> Upgrade pip + tooling..." -ForegroundColor Cyan
python -m pip install --upgrade pip wheel setuptools

Write-Host "==> Cài requirements.txt (CPU torch placeholder sẽ bị override ở bước sau)..." -ForegroundColor Cyan
pip install -r requirements.txt

Write-Host "==> Cài PyTorch cu128 (sm_120 cho RTX 50x)..." -ForegroundColor Cyan
# torch 2.7+ + cu128 = stable Blackwell support.
# --force-reinstall để override bất kỳ torch nào đã bị các dep khác kéo về.
pip install --upgrade --force-reinstall `
    torch torchvision torchaudio `
    --index-url https://download.pytorch.org/whl/cu128

Write-Host "==> Cài onnxruntime-gpu build CUDA 12..." -ForegroundColor Cyan
# Pip default cho onnxruntime-gpu hiện vẫn về build CUDA 11.
# Phải dùng 1.20+ và set provider rõ ràng nếu app dùng ONNX (musetalk có).
pip install --upgrade "onnxruntime-gpu>=1.20.0"

Write-Host "==> Smoke test: torch.cuda + compute capability..." -ForegroundColor Cyan
$smokeTest = @'
import torch
print(f"torch          = {torch.__version__}")
print(f"cuda available = {torch.cuda.is_available()}")
print(f"cuda version   = {torch.version.cuda}")
print(f"arch_list      = {torch.cuda.get_arch_list()}")
if torch.cuda.is_available():
    cap = torch.cuda.get_device_capability(0)
    name = torch.cuda.get_device_name(0)
    print(f"gpu[0]         = {name} sm_{cap[0]}{cap[1]}")
    if f"sm_{cap[0]}{cap[1]}" in torch.cuda.get_arch_list() or cap[0] < 12:
        print("OK: kernel khả dụng cho GPU này.")
    else:
        print(f"WARN: sm_{cap[0]}{cap[1]} không có trong arch_list — sẽ raise no-kernel-image.")
'@
python -c $smokeTest

Write-Host ""
Write-Host "==> Hoàn tất. Khởi chạy LiveTalking:" -ForegroundColor Green
Write-Host "    python app.py --model wav2lip --avatar_id wav2lip256_avatar1 --transport webrtc --tts edgetts --REF_FILE vi-VN-HoaiMyNeural"
