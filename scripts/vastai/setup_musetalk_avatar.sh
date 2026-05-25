#!/usr/bin/env bash
###############################################################################
#  setup_musetalk_avatar.sh — production-grade MuseTalk avatar preprocessing.
#
#  Wraps avatars/musetalk/genavatar.py (gốc MuseTalk repo) — dùng dwpose
#  landmark + face_parsing (bisent bypass elliptical mask) — chất lượng cao
#  nhất so với 2 path bypass cũ (mediapipe / S3FD-only).
#
#  Outputs (data/avatars/$AVATAR_ID/):
#    full_imgs/{:08d}.png        — raw frames (chèn watermark "LiveTalking")
#    coords.pkl                  — dwpose-refined face bbox per frame
#    latents.pt                  — 8-channel VAE latents (masked+ref concat)
#    mask/{:08d}.png             — elliptical jaw-cheek mask
#    mask_coords.pkl             — crop_box cho blending mượt
#    avator_info.json            — model=musetalk, fps, frames, bbox_shift
#
#  Idempotent: skip re-install mmpose nếu đã có; skip re-download dwpose ckpt;
#  KHÔNG skip preprocess (re-run sẽ overwrite avatar dir).
#
#  Usage:
#    bash scripts/vastai/setup_musetalk_avatar.sh [VIDEO] [AVATAR_ID]
#    # mặc định: data/uploads/mau.mp4  → mau
#
#  Env knobs (genavatar.py args):
#    VERSION=v15            v1 | v15            (v15 thêm extra_margin jaw)
#    BBOX_SHIFT=0           shift y của half-face landmark (- lên, + xuống)
#    EXTRA_MARGIN=10        v15 thêm y2 margin cho cằm
#    PARSING_MODE=jaw       raw | jaw            (mask geometry mode)
#    LEFT_CHEEK_WIDTH=90    elliptical mask width
#    RIGHT_CHEEK_WIDTH=90
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

# ─── 1. Resolve venv_talking Python ────────────────────────────────────────
if [[ -L venv_talking ]]; then
  TARGET=$(readlink -f venv_talking)
  PY="$TARGET/bin/python"
else
  PY="${REPO_ROOT}/venv_talking/bin/python"
fi
if [[ ! -x "$PY" ]]; then
  echo "[ERR] venv_talking chưa có. Chạy: bash scripts/vastai/setup.sh trước." >&2
  exit 1
fi
echo "[avatar] Python: $PY ($("$PY" --version 2>&1))"

# ─── 2. Install mmpose stack (idempotent) ─────────────────────────────────
# Skip openmim/mim CLI — openxlab dep pin setuptools~=60.2.0 + rich~=13.4.2
# gây py3.12 incompat (pkgutil.ImpImporter removed trong py3.12, pkg_resources
# crash khi import). Thay vì fight với openxlab, dùng openmmlab CDN pre-built
# wheels trực tiếp qua pip — match torch ABI bằng URL pattern.
if ! "$PY" -c "import mmpose, mmcv, mmengine, mmdet" 2>/dev/null; then
  # Detect torch version + cuda tag để pick đúng wheel
  TORCH_MAJORMIN=$("$PY" -c "import torch; v=torch.__version__.split('+')[0].rsplit('.',1)[0]; print(v)" 2>/dev/null)
  TORCH_CUDA_TAG=$("$PY" -c "import torch; print('cu'+torch.version.cuda.replace('.',''))" 2>/dev/null)
  MMCV_WHEEL_URL="https://download.openmmlab.com/mmcv/dist/${TORCH_CUDA_TAG}/torch${TORCH_MAJORMIN}/index.html"
  echo "[avatar] torch=${TORCH_MAJORMIN}+${TORCH_CUDA_TAG}, mmcv wheel index:"
  echo "         ${MMCV_WHEEL_URL}"

  # mmcv CHỈ install qua wheel index (built CUDA ops, không build from source
  # → tránh py3.12 + pkg_resources fail trong build isolation).
  "$PY" -m pip install --no-cache-dir 'mmcv>=2.0.1,<2.3' -f "${MMCV_WHEEL_URL}" \
    || { echo "[ERR] mmcv wheel không có cho torch${TORCH_MAJORMIN}/${TORCH_CUDA_TAG}"; \
         echo "      check: ${MMCV_WHEEL_URL}"; exit 1; }

  # mmengine + mmdet + mmpose pure Python — pip install thẳng.
  "$PY" -m pip install --no-cache-dir \
    "mmengine>=0.10,<1.0" \
    "mmdet>=3.1.0,<3.4" \
    "mmpose>=1.1.0,<1.4"
else
  echo "[avatar] mmpose stack đã có (skip install)"
fi
# face_alignment (preprocessing.py: from face_detection import FaceAlignment)
# — local bundle ở avatars/musetalk/utils/face_detection, không cần pip extra,
# nhưng s3fd weights load qua torch.utils.model_zoo.load_url (auto cache).

# ─── 3. Verify required model files ───────────────────────────────────────
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
    echo "[ERR] vẫn thiếu $f sau download_models.sh — check log HF/network." >&2
    exit 1
  fi
done

# ─── 4. Video precondition ────────────────────────────────────────────────
if [[ ! -f "$VIDEO" ]]; then
  echo "[ERR] video không tồn tại: $VIDEO" >&2
  echo "      scp từ local: scp -P <PORT> data/uploads/mau.mp4 root@<HOST>:/workspace/LiveTalking/data/uploads/" >&2
  exit 1
fi

# ─── 5. Run genavatar.py ──────────────────────────────────────────────────
# preprocessing.py dùng `from face_detection import FaceAlignment, LandmarksType`
# — không có dotted path. Đẩy avatars/musetalk/utils vào PYTHONPATH để resolve
# face_detection sub-package thành top-level.
echo "[avatar] === genavatar.py === id=$AVATAR_ID  video=$VIDEO  version=$VERSION"
PYTHONPATH="${REPO_ROOT}/avatars/musetalk/utils:${PYTHONPATH:-}" \
  "$PY" -m avatars.musetalk.genavatar \
    --file "$VIDEO" \
    --avatar_id "$AVATAR_ID" \
    --version "$VERSION" \
    --bbox_shift "$BBOX_SHIFT" \
    --extra_margin "$EXTRA_MARGIN" \
    --parsing_mode "$PARSING_MODE" \
    --left_cheek_width "$LEFT_CHEEK" \
    --right_cheek_width "$RIGHT_CHEEK"

# ─── 6. Patch avator_info.json ────────────────────────────────────────────
# genavatar.py chỉ ghi {avatar_id, video_path, bbox_shift}. Runtime
# (musetalk_avatar.py load_avatar) không yêu cầu nhưng start.sh + admin web
# đọc {model, fps, frames} để render UI / chọn pipeline.
"$PY" - "$AVATAR_ID" <<'PY'
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

# ─── 7. Sanity check output ────────────────────────────────────────────────
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
