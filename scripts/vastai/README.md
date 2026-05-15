# Deploy LiveTalking trên Vast.ai

Quy trình deploy nhanh — ~10 phút từ instance mới đến server chạy.

> **Transport hiện tại = `wsstream`** (MPEG-TS over WebSocket + JSMpeg). Chỉ cần
> map TCP port 8010 — không cần UDP, không cần TURN server, bypass NAT hoàn toàn.

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
| Port             | Map TCP **8010** ra public (tất cả traffic: web admin + WS video stream) |
| SSH key          | Add `id_ed25519.pub`/`vast_key.pub` qua "Manage SSH Keys" |
| On-start script  | (Optional) paste nội dung `scripts/vastai/onstart.sh` để tự clone repo + setup |

**KHÔNG cần map UDP port range** — wsstream chạy hoàn toàn qua TCP cùng port 8010.

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
4. SSH chạy `scripts/vastai/setup.sh`:
   - Apt deps (ffmpeg, build tools)
   - **Reuse /venv/main nếu Vast image đã có torch** (skip 2.5GB download)
   - **pip install requirements_vast.txt** (slim, Aliyun mirror, prebuilt llama-cpp wheel) → ~3-5 phút thay vì 20-30 phút
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
TTS_ENGINE=vieneu VIENEU_MODE=turbo \
AVATAR_MODEL=wav2lip AVATAR_ID=wav2lip256_avatar1 \
BRAIN_ENABLED=false \
bash scripts/vastai/start.sh
```

| Var               | Default                  | Ý nghĩa                                          |
| ----------------- | ------------------------ | ------------------------------------------------ |
| `AVATAR_MODEL`    | `wav2lip`                | `wav2lip` / `musetalk` / `ultralight`            |
| `AVATAR_ID`       | `wav2lip256_avatar1`     | Folder trong `data/avatars/`                     |
| `TTS_ENGINE`      | `vieneu`                 | `vieneu` (Apache 2.0, Vietnamese)                |
| `VIENEU_MODE`     | `turbo`                  | `turbo` / `standard` / `gpu` (lmdeploy) / `remote` |
| `TRANSPORT`       | `wsstream`               | `wsstream` / `virtualcam`                        |
| `LISTEN_PORT`     | `8010`                   | HTTP + WebSocket port                            |
| `BRAIN_ENABLED`   | `false`                  | Bật sales brain (cần LLM key)                    |
| `OPENAI_API_KEY`  | —                        | LLM key cho brain                                |
| `HF_TOKEN`        | —                        | Tăng rate limit khi pull VieNeu model HF         |

## 5. Test trong browser

### Option A — SSH tunnel (KHÔNG cần map port Vast UI) ⭐ Recommended

Từ máy Windows local, mở 1 cửa sổ PowerShell:

```powershell
ssh -i $env:USERPROFILE\.ssh\vast_key -p 36378 -L 8010:localhost:8010 root@<IP>
```

Giữ cửa sổ này open. Mở browser: **`http://localhost:8010/`** → tunnel sang Vast.

Pros: zero Vast UI config, hoạt động ngay, HTTPS không cần.

### Option B — Public port mapping qua Vast UI

1. Vast.AI dashboard → instance → Edit → thêm `8010` vào "Open ports"
2. Đợi Vast restart container → có PUBLIC_IPADDR + port mới
3. Mở `http://<PUBLIC_IPADDR>:<mapped_port>/`

### Test workflow

→ Tab **🔴 Live** → bấm **"▶ Kết nối preview"**.

JSMpeg lib auto-load từ jsDelivr CDN (~50KB) → connect WS tới
`/wsstream/0` → decode mpegts → render canvas.

Status overlay sẽ thành **"WSStream (~150ms)"** với chấm xanh khi stream OK.

Test lip-sync: gõ text vào **"💬 Chat thẳng với avatar"** → bấm "▶ Avatar nói".

## Troubleshooting

### `Torch not compiled with CUDA enabled` khi start VieNeu GPU mode

PyTorch wheel sai. Nếu RTX 50xx Blackwell, kiểm tra:

```bash
source venv_talking/bin/activate
python -c "import torch; print(torch.__version__, torch.version.cuda)"
# Mong đợi: 2.7+cu128 hoặc cao hơn (Blackwell), 2.4+cu121 (Ada/Ampere)
```

Reinstall:

```bash
pip install --index-url https://download.pytorch.org/whl/cu128 \
  --force-reinstall torch torchvision torchaudio
```

### Muốn dùng vieneu_mode=gpu (lmdeploy TurboMind)

LMDeploy tách khỏi core deps vì `lmdeploy[all]` force `torch<=2.10` (downgrade
torch cu128 trên Blackwell). Mặc định `VIENEU_MODE=turbo` (0.3B model, không cần
lmdeploy, chạy ngay).

Nếu muốn thử gpu mode:

```bash
source venv_talking/bin/activate
pip install lmdeploy  # KHÔNG dùng [all] để tránh torch downgrade
VIENEU_MODE=gpu bash scripts/vastai/start.sh
```

### WSStream không lên frame trong browser

- Check log server có `[WSStream] ffmpeg connected to video+audio sockets` không
- Check `[WSStream] N client(s) | avg fps=24.95` — N>0 nghĩa là browser đã connect
- Console browser DevTools: kiểm tra WS `ws://.../wsstream/0` không trả 404 / 400
- Nếu Vast.AI có proxy/load balancer, đảm bảo nó pass-through WebSocket upgrade

### Avatar không có (data/avatars/<id> missing)

Re-SCP từ Windows:

```powershell
.\scripts\vastai\deploy_from_windows.ps1 ... -SkipSetup
```

### Setup chậm vì pip download

```bash
export PIP_CACHE_DIR=/workspace/.pip-cache
export PIP_DEFAULT_TIMEOUT=120
```

## Files liên quan

- [setup.sh](setup.sh) — one-shot install trên instance (idempotent).
- [start.sh](start.sh) — production start, env-driven.
- [onstart.sh](onstart.sh) — paste vào field "On-start Script" khi tạo Vast instance.
- [download_models.sh](download_models.sh) — pull thêm musetalk/whisper khi cần.
- [deploy_from_windows.ps1](deploy_from_windows.ps1) — script Windows-side scp + ssh-execute.
