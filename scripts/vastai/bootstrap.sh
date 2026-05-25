#!/usr/bin/env bash
###############################################################################
#  bootstrap.sh — single entry point để setup LiveTalking trên Vast.AI mới.
#
#  Chạy LẦN ĐẦU sau khi `deploy_from_windows.ps1` đã scp code + assets lên:
#    bash scripts/vastai/bootstrap.sh
#
#  Tuần tự:
#    1. setup.sh                  — 2 venvs (talking/vieneu, fast mode default),
#                                    torch, pip deps. ~10-15 phút.
#                                    (3 venvs nếu SETUP_LMDEPLOY=true cho remote mode)
#    2. download_models.sh        — musetalkV15 + sd-vae + whisper + dwpose +
#                                    VieNeu-TTS pre-cache + wav2lip.pth.
#                                    ~5-10 phút.
#    3. setup_musetalk_avatar.sh  — uv-install py3.10, fresh venv_avatar,
#                                    mm* stack từ openmmlab wheels,
#                                    genavatar.py preprocess mau.mp4.
#                                    ~5-7 phút (chỉ chạy nếu AVATAR_VIDEO set).
#
#  Env knobs:
#    AVATAR_VIDEO=data/uploads/mau.mp4   # bỏ qua avatar setup nếu unset
#    AVATAR_ID=mau                       # avatar dir name trong data/avatars/
#
#  Sau khi bootstrap xong:
#    AVATAR_MODEL=musetalk AVATAR_ID=mau bash scripts/vastai/start.sh
#
#  Idempotent: re-run sẽ skip steps đã xong (mỗi script tự check).
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

AVATAR_VIDEO="${AVATAR_VIDEO:-data/uploads/mau.mp4}"
AVATAR_ID="${AVATAR_ID:-mau}"

echo "═════════════════════════════════════════════════════════════════"
echo " LiveTalking bootstrap"
echo "   AVATAR_VIDEO=$AVATAR_VIDEO"
echo "   AVATAR_ID=$AVATAR_ID"
echo "═════════════════════════════════════════════════════════════════"

echo
echo "▶ [1/3] setup.sh — 2 venvs (talking/vieneu, fast mode default)..."
bash scripts/vastai/setup.sh

echo
echo "▶ [2/3] download_models.sh — musetalkV15 + sd-vae + whisper + dwpose + VieNeu..."
bash scripts/vastai/download_models.sh

if [[ -f "$AVATAR_VIDEO" ]]; then
  echo
  echo "▶ [3/3] setup_musetalk_avatar.sh — preprocess $AVATAR_VIDEO → $AVATAR_ID..."
  bash scripts/vastai/setup_musetalk_avatar.sh "$AVATAR_VIDEO" "$AVATAR_ID"
else
  echo
  echo "▶ [3/3] SKIP avatar preprocess ($AVATAR_VIDEO không tồn tại)."
  echo "    scp video lên trước: scp -P <PORT> mau.mp4 root@<HOST>:$(pwd)/data/uploads/"
  echo "    Rồi chạy: bash scripts/vastai/setup_musetalk_avatar.sh data/uploads/mau.mp4 mau"
fi

echo
echo "═════════════════════════════════════════════════════════════════"
echo " ✓ Bootstrap xong. Start production stack:"
echo
echo "   AVATAR_MODEL=musetalk AVATAR_ID=$AVATAR_ID bash scripts/vastai/start.sh"
echo
echo "   Open: http://\$PUBLIC_IPADDR:8010/   (hoặc ssh -L 8010:localhost:8010)"
echo "═════════════════════════════════════════════════════════════════"
