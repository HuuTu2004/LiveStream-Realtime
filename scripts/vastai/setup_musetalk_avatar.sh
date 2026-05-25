#!/usr/bin/env bash
###############################################################################
#  setup_musetalk_avatar.sh — production-grade MuseTalk avatar preprocessing.
#
#  Wraps avatars/musetalk/genavatar.py (gốc MuseTalk) — DWPose landmark +
#  8-channel VAE latents + elliptical jaw-cheek mask (bisent bypass).
#
#  ┌──────────────────────────────────────────────────────────────────────┐
#  │  WHY a separate venv_avatar?                                          │
#  │                                                                       │
#  │  Vast.AI Ubuntu 24.04 base image = Python 3.12 + CUDA 12.x. MuseTalk  │
#  │  upstream stack (mmcv 2.0.1 / mmdet 3.1.0 / mmpose 1.1.0) only has    │
#  │  pre-built wheels for:                                                │
#  │      Python 3.10 + torch 2.0.1 + cu118                                │
#  │                                                                       │
#  │  Trying to install on py3.12 → mmcv source build needs CUDA compile  │
#  │  (264 .cpp/.cu files, ~30 min on contended box, often fails).        │
#  │  Trying py3.10 + torch 2.4 cu121 → openmmlab CDN has no cp310/cu121  │
#  │  wheel for mmcv 2.0.x → falls back to source = same issue.           │
#  │                                                                       │
#  │  Solution: uv-installed standalone Python 3.10 (no root, no apt) +   │
#  │  torch 2.0.1+cu118 + pre-built mmcv 2.0.1 cu118 cp310 wheel from     │
#  │  openmmlab CDN. Runtime is fine because driver 570+ supports CUDA    │
#  │  11.8 forward (back-compat).                                          │
#  └──────────────────────────────────────────────────────────────────────┘
#
#  Outputs (data/avatars/$AVATAR_ID/):
#    full_imgs/{:08d}.png        — raw frames (LiveTalking watermark)
#    coords.pkl                  — DWPose-refined face bbox per frame
#    latents.pt                  — 8-channel VAE latents (masked+ref concat)
#    mask/{:08d}.png             — elliptical jaw-cheek mask
#    mask_coords.pkl             — crop_box cho blending
#    avator_info.json            — model=musetalk, fps=25, frames=N
#
#  Idempotent:
#    - venv_avatar tồn tại → skip uv + venv creation
#    - mm* stack đã có → skip pip install
#    - s3fd ckpt đã cache → skip wget
#    - genavatar overwrite avatar dir (re-run = fresh preprocess)
#
#  Usage:
#    bash scripts/vastai/setup_musetalk_avatar.sh [VIDEO] [AVATAR_ID]
#    # default: data/uploads/mau.mp4 → avatar_id "mau"
#
#  Env knobs (genavatar.py args):
#    VERSION=v15            v1 | v15  (v15 thêm extra_margin cằm)
#    BBOX_SHIFT=0           shift y của half-face landmark (- lên, + xuống)
#    EXTRA_MARGIN=10        v15: thêm px y2 cho cằm
#    PARSING_MODE=jaw       raw | jaw (mask geometry mode)
#    LEFT_CHEEK_WIDTH=90    width vùng má elliptical mask
#    RIGHT_CHEEK_WIDTH=90
#
#  Disk: +6 GB cho venv_avatar (torch 2.0 cu118 + mm* stack)
#  Time: 5-7 phút lần đầu (chủ yếu là pip download), ~30s lần sau.
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VIDEO="${1:-data/uploads/mau.mp4}"
AVATAR_ID="${2:-mau}"
VERSION="${VERSION:-v15}"
BBOX_SHIFT="${BBOX_SHIFT:-0}"
EXTRA_MARGIN="${EXTRA_MARGIN:-10}"
PARSING_MODE="${PARSING_MODE:-jaw}"
LEFT_CHEEK="${LEFT_CHEEK_WIDTH:-90}"
RIGHT_CHEEK="${RIGHT_CHEEK_WIDTH:-90}"

VENV_AVATAR="${REPO_ROOT}/venv_avatar"
PY_AVATAR="${VENV_AVATAR}/bin/python"

# ─── 1. uv-install Python 3.10 (standalone, no root) ──────────────────────
# Lý do: Vast.AI ubuntu 24.04 base = py3.12. MuseTalk official wheels chỉ có
# cho py3.10. Apt install python3.10 cần ppa:deadsnakes + root + apt update
# → classifier blocks trên shared host. uv tải standalone build từ
# indygreg/python-build-standalone vào ~/.uv/python/, hoàn toàn userspace.
PY310=""
if [[ -x "$PY_AVATAR" ]]; then
  echo "[avatar] venv_avatar đã tồn tại — skip Python 3.10 install"
else
  # Cài uv vào venv_talking (sẵn có sau setup.sh)
  if [[ ! -x "${REPO_ROOT}/venv_talking/bin/uv" ]]; then
    echo "[avatar] Installing uv vào venv_talking..."
    "${REPO_ROOT}/venv_talking/bin/pip" install --no-cache-dir -q uv
  fi
  echo "[avatar] Installing Python 3.10 qua uv (~30s, ~30MB)..."
  "${REPO_ROOT}/venv_talking/bin/uv" python install 3.10
  PY310=$("${REPO_ROOT}/venv_talking/bin/uv" python find 3.10)
  echo "[avatar] Python 3.10: $PY310"
fi

# ─── 2. Tạo venv_avatar ───────────────────────────────────────────────────
if [[ ! -x "$PY_AVATAR" ]]; then
  echo "[avatar] Tạo venv_avatar tại $VENV_AVATAR..."
  "$PY310" -m venv "$VENV_AVATAR"
  "$PY_AVATAR" -m pip install --no-cache-dir -U pip
  # setuptools 75-79: có pkg_resources, không ref ImpImporter (py3.10 không
  # cần fix này nhưng giữ cho consistent với py3.12 path).
  "$PY_AVATAR" -m pip install --no-cache-dir -U 'setuptools>=75,<80' wheel
fi
echo "[avatar] Python: $PY_AVATAR ($("$PY_AVATAR" --version 2>&1))"

# ─── 3. Install torch 2.0.1 cu118 + MuseTalk official mm* stack ───────────
if ! "$PY_AVATAR" -c "import mmpose, mmcv, mmengine, mmdet" 2>/dev/null; then
  echo "[avatar] === MuseTalk official stack (torch 2.0.1 cu118 + mm*) ==="
  echo "[avatar]   Combo này khớp pre-built wheels openmmlab CDN."

  # Torch 2.0.1 cu118 — MuseTalk README spec. Driver >=525 (CUDA 11.8+ support)
  # đủ runtime. Vast.AI 3090 driver 570 OK ngược về cu11.
  echo "[avatar] torch 2.0.1+cu118 + torchvision 0.15.2..."
  "$PY_AVATAR" -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cu118 \
    torch==2.0.1 torchvision==0.15.2

  # mmcv 2.0.1 cu118 cp310 wheel — KHÔNG build source, KHÔNG CUDA compile.
  echo "[avatar] mmcv 2.0.1 cu118 cp310 (pre-built wheel)..."
  "$PY_AVATAR" -m pip install --no-cache-dir \
    mmcv==2.0.1 -f https://download.openmmlab.com/mmcv/dist/cu118/torch2.0/index.html

  # mmengine + mmdet 3.1.0 + mmpose 1.1.0 = MuseTalk official combo.
  echo "[avatar] mmengine + mmdet 3.1.0 + mmpose 1.1.0..."
  "$PY_AVATAR" -m pip install --no-cache-dir \
    mmengine 'mmdet==3.1.0' 'mmpose==1.1.0'

  # numpy<2 — mmcv 2.0.1 wheel pull numpy 2.x nhưng:
  #   1. torch 2.0.1 built cho numpy 1.x ABI → warning "_ARRAY_API not found"
  #   2. xtcocotools (dep mmpose) compile against numpy 1.x → ValueError
  #      "numpy.dtype size changed: 96 vs 88 bytes"
  echo "[avatar] pin numpy<2 (xtcocotools ABI compat)..."
  "$PY_AVATAR" -m pip install --no-cache-dir 'numpy<2'

  # diffusers/transformers/opencv cho VAE + UNet load trong genavatar.
  echo "[avatar] diffusers + transformers + opencv + tqdm..."
  "$PY_AVATAR" -m pip install --no-cache-dir \
    'diffusers>=0.27,<0.32' 'transformers==4.46.2' accelerate omegaconf einops \
    opencv-python-headless tqdm
else
  echo "[avatar] mm* stack đã có (skip install)"
fi

# ─── 4. Verify required model files ───────────────────────────────────────
NEED_DL=0
[[ ! -f models/dwpose/dw-ll_ucoco_384.pth ]] && NEED_DL=1
[[ ! -f models/musetalkV15/unet.pth      ]] && NEED_DL=1
[[ ! -f models/musetalkV15/musetalk.json ]] && NEED_DL=1
[[ ! -d models/sd-vae                    ]] && NEED_DL=1
if [[ $NEED_DL -eq 1 ]]; then
  echo "[avatar] Models thiếu — chạy download_models.sh..."
  bash scripts/vastai/download_models.sh
fi
for f in \
  models/dwpose/dw-ll_ucoco_384.pth \
  models/musetalkV15/unet.pth \
  models/musetalkV15/musetalk.json \
  models/sd-vae/config.json
do
  if [[ ! -e "$f" ]]; then
    echo "[ERR] vẫn thiếu $f sau download_models.sh." >&2
    exit 1
  fi
done

# ─── 5. Pre-fetch s3fd ckpt từ HF CDN (nhanh hơn 200x adrianbulat.com) ───
# face_detection auto-download s3fd-619a316812.pth (85 MB) từ adrianbulat.com
# (UK, ~500 KB/s từ VN/US). Pre-fetch từ HF camenduru/facexlib mirror
# (Fastly CDN, ~100 MB/s) tiết kiệm 3-5 phút mỗi lần preprocess.
TORCH_CACHE="${HOME}/.cache/torch/hub/checkpoints"
S3FD_CKPT="${TORCH_CACHE}/s3fd-619a316812.pth"
if [[ ! -f "$S3FD_CKPT" ]]; then
  mkdir -p "$TORCH_CACHE"
  echo "[avatar] Pre-fetch s3fd ckpt từ HF CDN (89 MB)..."
  wget -q --show-progress -O "$S3FD_CKPT" \
    "https://huggingface.co/camenduru/facexlib/resolve/main/s3fd-619a316812.pth" \
    || { rm -f "$S3FD_CKPT"; echo "[WARN] HF mirror fail — sẽ fallback adrianbulat.com (chậm)"; }
fi

# ─── 6. Video precondition ────────────────────────────────────────────────
if [[ ! -f "$VIDEO" ]]; then
  echo "[ERR] video không tồn tại: $VIDEO" >&2
  echo "      scp từ local: scp -P <PORT> data/uploads/mau.mp4 root@<HOST>:$REPO_ROOT/data/uploads/" >&2
  exit 1
fi

# ─── 7. Run genavatar.py với venv_avatar ──────────────────────────────────
# preprocessing.py dùng `from face_detection import FaceAlignment, LandmarksType`
# — không có dotted path. Đẩy avatars/musetalk/utils vào PYTHONPATH để
# resolve face_detection sub-package thành top-level.
echo "[avatar] === genavatar.py === id=$AVATAR_ID  video=$VIDEO  version=$VERSION"
PYTHONPATH="${REPO_ROOT}/avatars/musetalk/utils:${PYTHONPATH:-}" \
  "$PY_AVATAR" -m avatars.musetalk.genavatar \
    --file "$VIDEO" \
    --avatar_id "$AVATAR_ID" \
    --version "$VERSION" \
    --bbox_shift "$BBOX_SHIFT" \
    --extra_margin "$EXTRA_MARGIN" \
    --parsing_mode "$PARSING_MODE" \
    --left_cheek_width "$LEFT_CHEEK" \
    --right_cheek_width "$RIGHT_CHEEK"

# ─── 8. Patch avator_info.json ────────────────────────────────────────────
# genavatar.py ghi {avatar_id, video_path, bbox_shift}. Runtime
# (musetalk_avatar.py load_avatar) + admin web cần {model, fps, frames}
# để render UI / chọn pipeline.
"$PY_AVATAR" - "$AVATAR_ID" <<'PY'
import glob, json, os, sys
avatar_id = sys.argv[1]
info_path = f"data/avatars/{avatar_id}/avator_info.json"
try:
    with open(info_path) as f:
        info = json.load(f)
except Exception:
    info = {"avatar_id": avatar_id}
info["model"] = "musetalk"
info["fps"] = 25
info["frames"] = len(glob.glob(f"data/avatars/{avatar_id}/full_imgs/*.png"))
with open(info_path, "w", encoding="utf-8") as f:
    json.dump(info, f, indent=2, ensure_ascii=False)
print(f"[avatar] avator_info: {info}")
PY

# ─── 9. Sanity check output ───────────────────────────────────────────────
AVATAR_DIR="data/avatars/$AVATAR_ID"
for f in \
  "$AVATAR_DIR/full_imgs" \
  "$AVATAR_DIR/coords.pkl" \
  "$AVATAR_DIR/latents.pt" \
  "$AVATAR_DIR/mask" \
  "$AVATAR_DIR/mask_coords.pkl" \
  "$AVATAR_DIR/avator_info.json"
do
  if [[ ! -e "$f" ]]; then
    echo "[ERR] missing output: $f" >&2
    exit 1
  fi
done

N_FRAMES=$(ls "$AVATAR_DIR/full_imgs" | wc -l)
N_MASKS=$(ls "$AVATAR_DIR/mask" | wc -l)
echo
echo "═════════════════════════════════════════════════════════════════"
echo " ✓ MuseTalk avatar '$AVATAR_ID' đã sẵn sàng"
echo "   $N_FRAMES frames + $N_MASKS masks + latents.pt"
echo "   Dir: $AVATAR_DIR"
echo "═════════════════════════════════════════════════════════════════"
echo " Start production stack:"
echo "   AVATAR_MODEL=musetalk AVATAR_ID=$AVATAR_ID bash scripts/vastai/start.sh"
echo
