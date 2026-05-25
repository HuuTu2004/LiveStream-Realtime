# LiveTalking on Vast.AI — Setup Guide

> **Mục tiêu**: từ instance Vast.AI mới → server chạy musetalk + VieNeu TTS,
> stream MPEG-TS qua WebSocket. End-to-end **~25-30 phút**.

---

## 0. Kiến trúc

```
┌──────────────────────────────────────────────────────────────────┐
│  4 venv riêng (tránh ABI conflict torch/transformers/mmcv):       │
│                                                                   │
│  venv_lmdeploy   torch 2.4.1+cu121 + lmdeploy 0.9.0               │
│                  → :23333 /v1/chat/completions (Qwen3 bf16 backbone) │
│                                                                   │
│  venv_vieneu     torch 2.10+cu128 + vieneu[gpu] + ONNX codec      │
│                  → :23334 /infer_stream (text → PCM 24kHz)        │
│                                                                   │
│  venv_talking    torch 2.5+cu121 + diffusers <0.32 + wav2lip      │
│                  → :8010 / (web admin + wsstream MPEG-TS)         │
│                                                                   │
│  venv_avatar     torch 2.0.1+cu118 + mmcv 2.0.1 + mmpose 1.1      │
│                  → preprocessing only (MuseTalk avatar gen)        │
└──────────────────────────────────────────────────────────────────┘
```

**Browser chỉ cần TCP 8010** — wsstream over WebSocket, không cần UDP/TURN.

---

## 1. Tạo Vast.AI instance

| Param | Giá trị |
|---|---|
| **GPU** | RTX 3090/4090 24GB (an toàn) hoặc 5060+/5090 (Blackwell, cần cu128) |
| **Disk** | **≥ 60 GB** (stack ~30 GB + buffer) |
| **Docker image** | `nvidia/cuda:12.8.0-cudnn-devel-ubuntu24.04` hoặc `pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` |
| **Open ports** | TCP `8010` (browser); SSH tunnel cũng OK |
| **SSH key** | Add public key của bạn qua "Manage SSH Keys" |

---

## 2. Deploy code + assets từ Windows

Trên máy local Windows:

```powershell
cd C:\Users\Lucky\Downloads\StreamAI\LiveTalking
.\scripts\vastai\deploy_from_windows.ps1 `
  -InstanceHost <IP> -Port <PORT> -KeyPath $HOME\.ssh\vast_key
```

Script tự:
1. SSH test connection
2. `git clone/pull` repo `HuuTu2004/LiveStream-Realtime` lên `/workspace/LiveTalking`
3. SCP `models/wav2lip.pth` + `data/avatars/<id>/` nếu có

> ⚠️ **Nếu repo private, git clone fail trên remote**: thay vì git clone, dùng
> `tar -cf - --exclude=venv_* --exclude=models --exclude=data/avatars . | ssh ... 'tar -xf - -C /workspace/LiveTalking'` để stream code trực tiếp.

Cho avatar mới (chưa có `data/avatars/<id>/`), chỉ cần upload **raw video** vào `data/uploads/<name>.mp4`:

```powershell
scp -i $HOME\.ssh\vast_key -P <PORT> data\uploads\mau.mp4 root@<IP>:/workspace/LiveTalking/data/uploads/
```

---

## 3. Bootstrap (one command)

SSH vào remote, chạy:

```bash
ssh -i ~/.ssh/vast_key -p <PORT> root@<IP>
cd /workspace/LiveTalking
AVATAR_VIDEO=data/uploads/mau.mp4 AVATAR_ID=mau bash scripts/vastai/bootstrap.sh
```

Script chạy tuần tự 3 bước:

| Bước | Script | Thời gian | Output |
|---|---|---|---|
| 1 | `setup.sh` | ~10-15 phút | 3 venvs (talking/lmdeploy/vieneu) + torch + pip deps |
| 2 | `download_models.sh` | ~5-10 phút | musetalkV15 + sd-vae + whisper + dwpose + VieNeu-TTS cache |
| 3 | `setup_musetalk_avatar.sh` | ~5-7 phút | venv_avatar (py3.10 + mmcv) + genavatar.py preprocess |

**Idempotent** — re-run sẽ skip steps đã xong.

### Chạy từng bước thủ công (nếu cần debug)

```bash
bash scripts/vastai/setup.sh                                     # 3 venvs
bash scripts/vastai/download_models.sh                            # models
bash scripts/vastai/setup_musetalk_avatar.sh data/uploads/mau.mp4 mau  # avatar
```

---

## 4. Start server

```bash
AVATAR_MODEL=musetalk AVATAR_ID=mau bash scripts/vastai/start.sh
```

Stack lên thứ tự:
1. **lmdeploy api_server** → `127.0.0.1:23333` (Qwen3 backbone, ~30-90s load model lần đầu)
2. **vieneu_server.py** → `127.0.0.1:23334` (codec ONNX, ~10s warmup)
3. **app.py** → `0.0.0.0:8010` (web admin + wsstream)

Logs trong `logs/`: `lmdeploy.log`, `vieneu_server.log`, `server.log`.

### Env vars (override defaults)

| Var | Default | Ý nghĩa |
|---|---|---|
| `AVATAR_MODEL` | `wav2lip` | `wav2lip` / `musetalk` / `ultralight` |
| `AVATAR_ID` | `wav2lip256_avatar1` | Folder trong `data/avatars/` |
| `LISTEN_PORT` | `8010` | HTTP + WS port |
| `LMDEPLOY_PORT` | `23333` | lmdeploy backend port |
| `VIENEU_HTTP_PORT` | `23334` | vieneu_server port |
| `VIENEU_EMOTION` | `natural` | `natural` / `storytelling` |
| `VIENEU_VOICE_ID` | — | Preset voice ID (xem `/voices` endpoint) |
| `BRAIN_ENABLED` | `false` | Bật sales brain (cần `OPENAI_API_KEY`) |
| `TRANSPORT` | `wsstream` | `wsstream` (default) / `virtualcam` |

---

## 5. Test trong browser

### Option A — SSH tunnel ⭐ (zero Vast UI config)

```powershell
ssh -i $HOME\.ssh\vast_key -p <PORT> -L 8010:localhost:8010 root@<IP>
# Giữ cửa sổ này open
```

Browser: **`http://localhost:8010/`** → Tab "🔴 Live" → bấm "▶ Kết nối preview".

### Option B — Public port mapping

Vast.AI dashboard → Edit instance → thêm `8010` vào "Open ports" → đợi restart → mở `http://<PUBLIC_IPADDR>:<mapped_port>/`.

### Test lip-sync

Tab "💬 Chat thẳng với avatar" → gõ text → bấm "▶ Avatar nói" → quan sát môi avatar đồng bộ.

---

## File reference

| File | Mục đích |
|---|---|
| [bootstrap.sh](bootstrap.sh) | **Entry point one-command** — chạy setup + download + avatar |
| [setup.sh](setup.sh) | Step 1 — install 3 venvs (talking/lmdeploy/vieneu) |
| [download_models.sh](download_models.sh) | Step 2 — pull model weights từ HF |
| [setup_musetalk_avatar.sh](setup_musetalk_avatar.sh) | Step 3 — preprocess video → musetalk avatar |
| [start.sh](start.sh) | Production launcher — env-driven, spawn 3 process |
| [vieneu_server.py](vieneu_server.py) | venv_vieneu HTTP server (text → PCM stream) |
| [vieneu_chat_template.json](vieneu_chat_template.json) | lmdeploy custom passthrough chat template |
| `requirements_*.txt` | Pip pin per venv |
| [deploy_from_windows.ps1](deploy_from_windows.ps1) | Windows-side push code+assets |
| [encode_voice.py](encode_voice.py) | (Optional) voice clone — gen voice.pkl từ ref.wav |
| [onstart.sh](onstart.sh) | (Optional) paste vào "On-start Script" của Vast UI |
| [install_systemd.sh](install_systemd.sh) + [livetalking.service](livetalking.service) | (Optional) systemd unit cho non-Vast deploy |

---

## Troubleshooting

### `Setup.sh` chậm vì pip mirror

**Đã fix**: setup.sh dùng `pypi.org` (Fastly CDN, ~50-100 Mbps từ VN/US). Trước đây aliyun mirror throttle ~1 MB/s.

### `mmcv build wheel failed` khi setup_musetalk_avatar

**Đã fix**: script tạo venv_avatar py3.10 riêng + dùng pre-built wheel openmmlab cu118/torch2.0 cp310. Không build source.

### `Dinov2WithRegistersConfig` import error khi start app.py

**Đã fix**: requirements_vast.txt pin `diffusers>=0.27,<0.32` (compat transformers 4.46.2).

### Genavatar.py stuck tải s3fd

**Đã fix**: script pre-fetch từ `camenduru/facexlib` HF mirror (~100 MB/s) thay vì adrianbulat.com (~500 KB/s).

### Avatar không có (`data/avatars/<id>/` missing)

Re-SCP từ Windows hoặc preprocess lại:

```bash
bash scripts/vastai/setup_musetalk_avatar.sh data/uploads/mau.mp4 mau
```

### `vieneu_server` chết khi start

Check `logs/vieneu_server.log`. Lỗi thường gặp:

| Symptom | Fix |
|---|---|
| `OOM khi load model` | GPU < 16GB → giảm tải hoặc dùng GPU 24GB+ |
| `Torch not compiled with CUDA enabled` | venv_vieneu cài sai torch index — rerun `setup.sh` |
| `_torchaudio.abi3.so / libcudart.so.13` | torchaudio mismatch — `pip install --index-url cu128 torchaudio==<torch ver>` |

### WSStream không lên frame

- Server log có `[WSStream] ffmpeg connected to video+audio sockets`?
- Browser DevTools: WS `ws://.../wsstream/0` không 404?
- Vast.AI proxy phải pass-through WebSocket Upgrade header.

### Avatar bị nhân tạo (audio không khớp môi)

- Re-run preprocess với `EXTRA_MARGIN=15 BBOX_SHIFT=-5` để bbox bao trùm cằm hơn
- Hoặc `PARSING_MODE=raw` thay vì `jaw` cho mask rộng hơn

---

## Disk usage breakdown

| Component | Size |
|---|---|
| `venv_talking` | ~6.6 GB |
| `venv_lmdeploy` | ~6.0 GB |
| `venv_vieneu` | ~9.2 GB |
| `venv_avatar` | ~6.0 GB |
| `models/` (musetalkV15 + sd-vae + whisper + dwpose + VieNeu cache + wav2lip.pth) | ~10 GB |
| `data/avatars/<id>/` (264 frames + masks + latents) | ~180 MB |
| **Tổng** | **~38 GB** |

Disk Vast.AI **60 GB** đủ thoải mái. Tránh disk 50 GB (sát).

---

## Audio quality history (bugs đã fix, bake-in production)

| Symptom | Root cause | Fix |
|---|---|---|
| Tạch + clip + DRC distortion | int16 cast legacy RTMP trong f32le pipeline | Bỏ cast, float32 [-1,1] xuyên suốt |
| Tạch periodic 200ms | scipy resample_poly chunk-boundary transient | `soxr.ResampleStream` (stateful HQ) |
| Silence interleave 35% | ASR drain quá nhanh, queue empty | `WSStream.get_buffer_size()` throttle render |
| Gap 200-500ms giữa câu | Client split text per sentence, mỗi câu có TTFB | Server-side batch infer per sentence |
| Rè codec | distill-neucodec PyTorch incompat | Switch `neuphonic/neucodec-onnx-decoder-int8` (5x faster + clean) |
| lmdeploy "no Qwen3 rewrite" | lmdeploy <0.9 thiếu Qwen3 arch | Pin `lmdeploy==0.9.0` |
| lmdeploy "base template chat" | Vieneu raw completion, không có chat template | Custom passthrough `vieneu_chat_template.json` |
