# Deploy LiveTalking trên Vast.ai

Quy trình deploy nhanh — ~10-15 phút từ instance mới đến server chạy.

> **Transport = `wsstream`** (MPEG-TS over WebSocket + JSMpeg). Chỉ cần TCP
> port 8010 — không cần UDP, không cần TURN server, bypass NAT hoàn toàn.

> **TTS = `vieneu_http`** production 3-venv. Author defaults +
> hybrid batch-streaming với ONNX codec → 5x realtime + audio sạch (no rè).

## Architecture (3 venv, 3 process trên 1 instance Vast.ai)

```
┌──────────────────────────────────────────────────────────────┐
│ Process A: lmdeploy api_server  (venv_lmdeploy, torch 2.4)   │
│   :23333 /v1/chat/completions                                 │
│   VieNeu-TTS-v2 backbone (Qwen3 bfloat16, TurboMind)          │
│   --chat-template scripts/vastai/vieneu_chat_template.json    │
└──────────────────────────────────────────────────────────────┘
                          ↓ HTTP OpenAI API (text → audio_tokens)
┌──────────────────────────────────────────────────────────────┐
│ Process B: vieneu_server.py     (venv_vieneu, torch 2.6)     │
│   :23334 /infer_stream                                        │
│   Codec ONNX int8 (5x realtime, clean)                        │
│   Hybrid: server split sentences → tts.infer() batch mỗi câu │
│   → stream HTTP response chunks 200ms                         │
└──────────────────────────────────────────────────────────────┘
                          ↓ HTTP length-prefixed f32le PCM 24kHz
┌──────────────────────────────────────────────────────────────┐
│ Process C: app.py               (venv_talking, torch 2.4)    │
│   :8010 /                                                     │
│   tts/vieneu_http.py → soxr resample 24→16kHz                 │
│   wav2lip avatar lip-sync + wsstream MPEG-TS + WebSocket      │
└──────────────────────────────────────────────────────────────┘
```

## TTS pipeline knobs (production-tuned)

| Component | Setting | Reason |
|-----------|---------|--------|
| Backbone | lmdeploy 0.9.0 TurboMind bfloat16 | Author recommend, supports Qwen3 |
| Chat template | Custom passthrough JSON | Model là raw completion, KHÔNG có chat_template |
| Codec | `neuphonic/neucodec-onnx-decoder-int8` | 5x realtime + sạch hơn PyTorch trên VieNeu-TTS-v2 |
| Gen mode | `tts.infer()` per sentence (NOT `infer_stream`) | infer_stream có chunk-boundary artifact |
| Sampling | temp=1.0, top_k=50, rep_penalty=1.2 | Author API defaults |
| Resample 24→16k | `soxr.ResampleStream` stateful HQ | scipy resample_poly có FIR transient ở biên |
| Pre-buffer client | 0.5s | Absorb chunk arrival jitter; ONNX gen 5x realtime nên không cần lớn hơn |
| Audio dtype | float32 [-1, 1] xuyên suốt | Bỏ int16 cast legacy RTMP |
| wsstream audio queue | `get(timeout=12ms)` + `get_buffer_size()` | Absorb GPU batch jitter, throttle render loop |
| ffmpeg MP2 | 256kbps + `aresample=soxr` HQ | Mono speech transparent, đỡ rè internal resample |

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
   - **`venv_talking`** — reuse /venv/main (Vast template) hoặc /opt/conda (pytorch image) nếu có torch sẵn; else create fresh. Pip install `requirements_vast.txt` (slim — chỉ aiohttp/wav2lip deps).
   - **`venv_vieneu`** — luôn tạo fresh venv (cách ly torch 2.6+). Pip install `requirements_vieneu.txt` = `vieneu[gpu]` + `lmdeploy`.
   - Aliyun mirror → ~5-10 phút total thay vì 30-40 phút.
5. Verify `torch.cuda.is_available()` trên **cả 2 venv**.

Flags hữu ích:

| Flag           | Khi nào                                         |
| -------------- | ----------------------------------------------- |
| `-SkipAssets`  | Đã upload model + avatar (lần chạy lại)        |
| `-SkipSetup`   | Chỉ muốn re-sync code (đã cài deps trước)      |
| `-AvatarId X`  | Upload avatar dir khác (default `wav2lip256_avatar1`) |

## 4. (Optional) Tạo MuseTalk avatar chất lượng cao

Nếu muốn dùng `AVATAR_MODEL=musetalk` (chất lượng lipsync tốt nhất, **chậm hơn wav2lip ~30%** nhưng môi sắc nét + blend mượt), preprocess video raw qua wrapper sau:

```bash
ssh -i ~/.ssh/vast_key -p 56020 root@171.226.34.64
cd /workspace/LiveTalking

# scp video lên trước (1 lần):
# scp -P 56020 data/uploads/mau.mp4 root@HOST:/workspace/LiveTalking/data/uploads/

bash scripts/vastai/setup_musetalk_avatar.sh data/uploads/mau.mp4 mau
```

Script tự làm:

1. **Install mmpose stack** vào `venv_talking` qua openmim (idempotent) — `mmengine + mmcv + mmdet + mmpose` theo torch ABI 2.4 cu121.
2. **Download checkpoints** còn thiếu qua `download_models.sh`:
   - `models/dwpose/dw-ll_ucoco_384.pth` (~150MB, từ `TMElyralab/MuseTalk`)
   - `models/musetalkV15/{unet.pth, musetalk.json}`
   - `models/sd-vae/` (VAE encoder cho latents)
3. **Run `avatars/musetalk/genavatar.py`** — gốc MuseTalk pipeline:
   - DWPose landmark detection → refined face bbox (chính xác hơn S3FD/mediapipe ở vùng mép môi)
   - VAE encode 8-channel latents (masked + ref concat) cho UNet input
   - Geometric elliptical mask (bisent bypass) — mask vùng MIỆNG+CẰM, exclude cổ → không shake ở seam
4. **Patch `avator_info.json`** thêm `model="musetalk"` cho runtime nhận đúng pipeline.

Output dir `data/avatars/$AVATAR_ID/`:

```
full_imgs/{:08d}.png         raw frames + watermark "LiveTalking"
coords.pkl                   dwpose-refined bbox per frame
latents.pt                   8-channel VAE latents (masked+ref)
mask/{:08d}.png              elliptical jaw-cheek mask
mask_coords.pkl              crop_box cho image_prepare_material
avator_info.json             {"model": "musetalk", "fps": 25, "frames": N}
```

Env knobs (xem comment đầu script):

| Var | Default | Ý nghĩa |
|---|---|---|
| `VERSION` | `v15` | `v15` thêm `extra_margin` ở y2 cho cằm; `v1` cổ điển |
| `BBOX_SHIFT` | `0` | Shift y của half-face landmark (+ xuống, - lên) |
| `EXTRA_MARGIN` | `10` | v15: thêm px ở y2 cho cằm khỏi bị cắt |
| `PARSING_MODE` | `jaw` | `jaw` / `raw` — mode mask geometry |
| `LEFT_CHEEK_WIDTH` / `RIGHT_CHEEK_WIDTH` | `90` | Width vùng má elliptical mask |

**Thời gian preprocess (RTX 3090)**: ~0.4s/frame → 1 phút video 25fps = ~10 phút. Chạy 1 lần per avatar, sau đó runtime dùng cached output không cần mmpose nữa.

## 5. Start server

```bash
ssh -i ~/.ssh/vast_key -p 56020 root@171.226.34.64
cd /workspace/LiveTalking
bash scripts/vastai/start.sh
```

Server log → `server.log`. Truy cập admin tại `http://<PUBLIC_IPADDR>:8010/`
(PUBLIC_IPADDR là env Vast inject — `echo $PUBLIC_IPADDR` để xem).

### Env vars override

```bash
TTS_ENGINE=vieneu_http VIENEU_MODE=gpu \
AVATAR_MODEL=wav2lip AVATAR_ID=wav2lip256_avatar1 \
BRAIN_ENABLED=false \
bash scripts/vastai/start.sh
```

| Var                | Default                  | Ý nghĩa                                          |
| ------------------ | ------------------------ | ------------------------------------------------ |
| `AVATAR_MODEL`     | `wav2lip`                | `wav2lip` / `musetalk` / `ultralight`            |
| `AVATAR_ID`        | `wav2lip256_avatar1`     | Folder trong `data/avatars/`                     |
| `TTS_ENGINE`       | `vieneu_http`            | `vieneu_http` (production multi-venv) / `vieneu` (in-process legacy) |
| `VIENEU_MODE`      | `gpu`                    | `gpu` (lmdeploy bfloat16 — KHUYẾN NGHỊ) / `standard` / `turbo` |
| `VIENEU_HTTP_PORT` | `23334`                  | Port vieneu_server.py listen                     |
| `VIENEU_PORT`      | `23333`                  | Port LMDeploy backend bên trong venv_vieneu      |
| `TRANSPORT`        | `wsstream`               | `wsstream` / `virtualcam`                        |
| `LISTEN_PORT`      | `8010`                   | HTTP + WebSocket port (LiveTalking)              |
| `BRAIN_ENABLED`    | `false`                  | Bật sales brain (cần LLM key)                    |
| `OPENAI_API_KEY`   | —                        | LLM key cho brain                                |
| `HF_TOKEN`         | —                        | Tăng rate limit khi pull VieNeu model HF         |

## 6. Test trong browser

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

## Audio quality — bugs đã fix (history)

Các vấn đề audio phổ biến + fix tương ứng (đã bake-in vào production):

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Avatar im lặng dù `/human` trả `code:0` | `type='chat'` đi qua LLM brain — fail 401 khi `LLM_API_KEY=none` | `server/routes.py` fallback echo khi no key |
| Tạch + clip + DRC distortion | Legacy RTMP `int16 * 32767` cast trong `base_avatar.py` nhưng wsstream input là f32le | Bỏ cast, giữ float32 [-1,1] |
| Tạch periodic mỗi 200ms | `scipy.signal.resample_poly` chunk-by-chunk → FIR transient ở biên | Switch `soxr.ResampleStream` (stateful) |
| Silence interleave 35% trong audio | ASR.run_step drain quá nhanh, asr.queue empty → 10ms timeout chèn silence frame | `WSStream.get_buffer_size()` = qsize//2 → render loop throttle |
| Gap 200-500ms giữa sentences | vieneu_http split text rồi gọi từng câu, mỗi câu có TTFB 0.5s | Server tự split, không split ở client |
| Tẹt tẹt subtle pattern | `vieneu.infer_stream()` decode mỗi chunk độc lập → boundary artifacts | Server dùng `tts.infer()` batch per sentence, KHÔNG `infer_stream` |
| Rè codec output | `neuphonic/distill-neucodec` PyTorch full incompat với lmdeploy LM tokens | Switch `neuphonic/neucodec-onnx-decoder-int8` (5x faster + clean) |
| TTS thread chết sau exception | `BaseTTS.process_tts` không catch unhandled exception | Wrap `txt_to_audio` trong try/except outer |
| Audio underrun chèn silence 40ms | `wsstream._write_audio_chunk` dùng `get_nowait()` | `get(timeout=12ms)` blocking grace |
| lmdeploy load model fail "no Qwen3 rewrite" | lmdeploy <0.9 không có Qwen3 architecture support | Pin `lmdeploy==0.9.0` |
| lmdeploy "base template chat task" error | Vieneu gửi `/chat/completions`, lmdeploy mặc định yêu cầu template | Custom passthrough JSON `vieneu_chat_template.json` |

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

### Muốn fallback in-process legacy (`TTS_ENGINE=vieneu`)

Production mặc định là `vieneu_http` (2 venv tách biệt). Nếu muốn quay về
legacy single-venv mode — chỉ debug, không khuyến nghị production:

```bash
source venv_talking/bin/activate
pip install vieneu[gpu] lmdeploy   # cài thẳng vào venv_talking (có thể conflict với wav2lip torch)
TTS_ENGINE=vieneu VIENEU_MODE=gpu bash scripts/vastai/start.sh
```

### `vieneu_server` chết khi start

Check `logs/vieneu_server.log`. Lỗi thường gặp:

- `OOM khi load model` — giảm `VIENEU_TP` hoặc dùng GPU ≥ 24GB VRAM.
- `lmdeploy died early` — kiểm tra `nvidia-smi`, có process khác chiếm port 23333.
- `Torch not compiled with CUDA enabled` — venv_vieneu cài sai torch index. Rerun
  `setup.sh` (nó force reinstall torch theo arch).

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
