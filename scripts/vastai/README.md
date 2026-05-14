# Deploy LiveTalking trên Vast.ai

Quy trình deploy nhanh — ~10 phút từ instance mới đến server chạy.

## TL;DR

```powershell
# Từ Windows local, sau khi commit + push code:
cd C:\Users\Lucky\Downloads\StreamAI\LiveTalking
.\scripts\vastai\deploy_from_windows.ps1 `
  -InstanceHost 171.226.34.64 `
  -Port 56020 `
  -KeyPath $HOME\.ssh\vast_key
```

Sau khi script chạy xong:

```bash
ssh -i ~/.ssh/vast_key -p 56020 root@171.226.34.64
cd /workspace/LiveTalking && bash scripts/vastai/start.sh
# Open: http://<PUBLIC_IPADDR>:8010/
```

---

## 1. Tạo instance Vast.ai

| Yêu cầu          | Giá trị                                       |
| ---------------- | --------------------------------------------- |
| GPU              | RTX 3090/4090 24GB (an toàn) hoặc 5060+/5090 (Blackwell, cần CUDA 12.8) |
| Disk             | ≥ 50GB                                        |
| Docker image     | `nvidia/cuda:12.8.0-cudnn-devel-ubuntu24.04` hoặc `pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` |
| Port             | Map TCP **8010** ra public (web admin + signaling) |
| SSH key          | Add `id_ed25519.pub`/`vast_key.pub` qua "Manage SSH Keys" |
| On-start script  | (Optional) paste nội dung `scripts/vastai/onstart.sh` để tự clone repo + setup |

WebRTC media qua UDP — cần map UDP range cho instance, hoặc chấp nhận
fallback TURN. Xem mục **WebRTC trên Vast** bên dưới.

## 2. Lấy SSH info

Vào instance Vast trên web UI → **Connect** → copy "Direct ssh connect":

```
ssh -p 56020 root@171.226.34.64
```

→ `-Port 56020`, `-InstanceHost 171.226.34.64`.

## 3. Chạy deploy script (Windows)

```powershell
cd C:\Users\Lucky\Downloads\StreamAI\LiveTalking
.\scripts\vastai\deploy_from_windows.ps1 `
  -InstanceHost 171.226.34.64 -Port 56020 `
  -KeyPath $HOME\.ssh\vast_key
```

Script tự làm:

1. SSH test connection.
2. `git clone` repo (hoặc `git pull` nếu đã có).
3. SCP `models/wav2lip.pth` (~205MB) + `data/avatars/wav2lip256_avatar1/` (~363MB) lên instance.
4. SSH chạy `scripts/vastai/setup.sh` → cài apt deps, tạo venv, cài PyTorch + CUDA 12.8 (auto-detect RTX 50xx → cu128, RTX 30/40xx → cu121), cài `requirements.txt`.
5. Verify `torch.cuda.is_available()` + GPU name + sm_xx.

Flags hữu ích:

| Flag           | Khi nào                                         |
| -------------- | ----------------------------------------------- |
| `-SkipAssets`  | Đã upload model + avatar (lần chạy lại)        |
| `-SkipSetup`   | Chỉ muốn re-sync code (đã cài deps trước)      |
| `-AvatarId X`  | Upload avatar dir khác (default `wav2lip256_avatar1`) |

## 4. Start server

```bash
ssh -i ~/.ssh/vast_key -p 56020 root@171.226.34.64
cd /workspace/LiveTalking
bash scripts/vastai/start.sh
```

Server log → `server.log`. Truy cập admin tại `http://<PUBLIC_IPADDR>:8010/`
(PUBLIC_IPADDR là env Vast inject — `echo $PUBLIC_IPADDR` để xem).

### Env vars override

```bash
TTS_ENGINE=vieneu VIENEU_MODE=gpu \
AVATAR_MODEL=wav2lip AVATAR_ID=wav2lip256_avatar1 \
BRAIN_ENABLED=false \
bash scripts/vastai/start.sh
```

| Var               | Default                  | Ý nghĩa                                          |
| ----------------- | ------------------------ | ------------------------------------------------ |
| `AVATAR_MODEL`    | `wav2lip`                | `wav2lip` / `musetalk` / `ultralight`            |
| `AVATAR_ID`       | `wav2lip256_avatar1`     | Folder trong `data/avatars/`                     |
| `TTS_ENGINE`      | `vieneu`                 | `vieneu` (Apache 2.0, Vietnamese)                |
| `VIENEU_MODE`     | `gpu`                    | `gpu` (lmdeploy) / `standard` / `turbo` / `remote` |
| `TRANSPORT`       | `webrtc`                 | `webrtc` / `rtmp` / `virtualcam` / `rtcpush`     |
| `LISTEN_PORT`     | `8010`                   | Web + signaling                                  |
| `BRAIN_ENABLED`   | `false`                  | Bật sales brain (cần LLM key)                    |
| `OPENAI_API_KEY`  | —                        | LLM key cho brain                                |
| `HF_TOKEN`        | —                        | Tăng rate limit khi pull VieNeu model HF         |

## 5. Test WebRTC

Mở browser: `http://<PUBLIC_IPADDR>:8010/`
→ Tab **Live** → bấm **Kết nối WebRTC**.

ICE config tự fetch qua `GET /ice-config` (STUN Google + Cloudflare, TURN
nếu set env `TURN_URL/TURN_USER/TURN_PASS`).

SDP munging: server tự thay private IP (10.x/172.x) bằng `$PUBLIC_IPADDR`
trong answer SDP — xem [server/rtc_manager.py](../../server/rtc_manager.py).

## Troubleshooting

### `Torch not compiled with CUDA enabled` khi start VieNeu GPU mode

PyTorch wheel sai. Nếu RTX 50xx Blackwell, kiểm tra:

```bash
source venv_talking/bin/activate
python -c "import torch; print(torch.__version__, torch.version.cuda)"
# Mong đợi: 2.7+cu128 hoặc cao hơn
```

Nếu không, reinstall:

```bash
pip install --index-url https://download.pytorch.org/whl/cu128 \
  --force-reinstall torch torchvision torchaudio
```

### Muốn dùng vieneu_mode=gpu (lmdeploy TurboMind)

LMDeploy bị tách khỏi core deps vì `lmdeploy[all]` force `torch<=2.10`
→ downgrade torch cu128 trên Blackwell. Mặc định `VIENEU_MODE=turbo`
(0.3B model, không cần lmdeploy, chạy ngay với torch cu128).

Nếu muốn thử gpu mode (max throughput, nhưng chưa stable trên Blackwell):

```bash
source venv_talking/bin/activate
pip install lmdeploy  # KHÔNG dùng [all] để tránh torch downgrade
# Nếu pip vẫn cố downgrade torch, force:
#   pip install lmdeploy --no-deps && pip install <missing deps thủ công>

VIENEU_MODE=gpu bash scripts/vastai/start.sh
```

Nếu LMDeploy server không sẵn sàng sau 180s → kernel sm_120 chưa
được biên dịch trong version lmdeploy đó. Quay về:

```bash
VIENEU_MODE=turbo bash scripts/vastai/start.sh
```

### `/offer` trả 500 — avatar không tồn tại

Avatar dir `data/avatars/<AVATAR_ID>` không có. Re-SCP:

```powershell
.\scripts\vastai\deploy_from_windows.ps1 ... -SkipSetup
```

### WebRTC ICE fail / video không hiện

Vast NAT không route được UDP ephemeral. Cần TURN:

```bash
export TURN_URL='turn:turn.example.com:3478?transport=udp'
export TURN_USER='username'
export TURN_PASS='password'
bash scripts/vastai/start.sh
```

Test thử với Google TURN public hoặc thuê [metered.ca free](https://www.metered.ca/tools/openrelay/).

### Setup chậm vì pip download

Bật pip cache + parallel:

```bash
export PIP_CACHE_DIR=/workspace/.pip-cache
export PIP_DEFAULT_TIMEOUT=120
```

## Files liên quan

- [setup.sh](setup.sh) — one-shot install trên instance (idempotent).
- [start.sh](start.sh) — production start, env-driven.
- [onstart.sh](onstart.sh) — paste vào field "On-start Script" khi tạo Vast instance.
- [download_models.sh](download_models.sh) — pull thêm musetalk/whisper/f5tts khi cần.
- [deploy_from_windows.ps1](deploy_from_windows.ps1) — script Windows-side để scp + ssh-execute.
