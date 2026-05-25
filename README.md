# LiveTalking Sales

> Digital-human livestream bán hàng tiếng Việt: avatar lip-sync + F5-TTS voice cloning + LLM sales brain + TikTok scraper. Tất cả trong 1 process, 1 trang web quản trị.

<p align="center">
  <img src="./assets/LiveTalking-logo.jpg" align="middle" width="280"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue">
  <img src="https://img.shields.io/badge/python-3.10+-aff.svg">
  <img src="https://img.shields.io/badge/cuda-12.1-green">
  <img src="https://img.shields.io/badge/Vietnamese-Optimized-red">
</p>

> Dựa trên [lipku/LiveTalking](https://github.com/lipku/LiveTalking) — mở rộng cho livestream-sales tiếng Việt.

## ✨ Tính năng chính

- 🔴 **Live TikTok 1-click**: nhập `@username` → tự cào comment/like/gift qua [TikTokLive](https://pypi.org/project/TikTokLive/) → đưa vào LLM brain trả lời realtime
- 🧠 **Sales brain**: persona Linh Sài Gòn, 8-stage SALES_CYCLE, intent classifier 6 cats, silence trigger, viewer milestones
- 🎙️ **TTS Vietnamese** — 2 lựa chọn:
  - **VieNeu-TTS** (default, **Apache 2.0**, voice clone 3-5s, code-switching Vi-En, 6 preset voices)
    - Mode `gpu` (default): plugin tự spawn **LMDeploy TurboMind** local server → throughput max nhờ FlashAttn + paged KV cache + tensor parallel
    - Mode `standard`: GGUF+ONNX cho deploy không GPU
    - Mode `turbo`: 0.3B variant 2x nhanh
  - **F5-TTS** (chất lượng cảm xúc cao hơn, CC-BY-NC-SA — chỉ research)
- 🎥 **Avatar lip-sync**: MuseTalk (chất lượng cao) / Wav2Lip (nhanh) / Ultralight (mobile)
- 👋 **Gesture system**: LLM tự inject `[wave]`/`[point]`/`[nod]`/`[smile]` đồng bộ với câu nói
- 📦 **Product CRUD**: schema linh hoạt, bán bất kỳ ngành hàng nào
- 🛠️ **Studio portal**: upload + train avatar/voice/gesture qua web UI
- ⚙️ **Dynamic config**: chỉnh 22 tham số qua web, không cần restart cho phần lớn

## 🚀 Quick start

### Cài đặt

```bash
git clone <repo> LiveTalking
cd LiveTalking
pip install -r requirements.txt
```

### Khởi động

```bash
export OPENAI_API_KEY=sk-...                       # hoặc Ollama: export LLM_URL=http://host:11434/v1
bash scripts/vastai/start.sh                       # production mode (env-driven)

# Hoặc trực tiếp (VieNeu GPU mode, default):
python app.py --model wav2lip --avatar_id demo --tts vieneu --vieneu_mode gpu \
  --transport webrtc --brain_enabled true \
  --vieneu_ref_audio data/avatars/demo/voice/ref.wav

# Multi-GPU (2 GPU):
python app.py --tts vieneu --vieneu_mode gpu --vieneu_tp 2 ...

# Không GPU (CPU only):
python app.py --tts vieneu --vieneu_mode standard ...

# Đổi sang F5-TTS (non-commercial):
python app.py --tts f5tts --f5_ref_audio data/avatars/demo/voice/ref.wav ...
```

**Lần đầu start VieNeu GPU mode**: plugin spawn `lmdeploy serve api_server` ở port 23333,
chờ load model `pnnbao-ump/VieNeu-TTS-v2` lên GPU (~30-60s). Các lần sau cached.

### Truy cập trang quản trị

`http://localhost:8010/` — SPA 5 tab:

| Tab | Chức năng |
|---|---|
| 🔴 **Live** | TikTok auto-scrape + WebRTC viewer + comment feed realtime + stats |
| 📦 **Sản phẩm** | CRUD bất kỳ mặt hàng nào (quần áo, điện tử, mỹ phẩm…) |
| 🎥 **Video** | Upload + preprocess + train avatar + gesture pack |
| 🎙️ **Âm thanh** | Upload reference voice cho F5-TTS + test TTS |
| ⚙️ **Cài đặt** | 22 config field dynamic (brain/llm/tts/avatar/server/studio) |

## 🌐 Deploy Vast.AI

Vast.AI đã chạy trên Docker container, không cần build Dockerfile riêng:

1. Tạo instance với base image `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime`
2. Mount volume `/workspace` để persist models qua restart
3. Expose port `8010`
4. SSH vào, chạy:
   ```bash
   cd /workspace && git clone <repo> LiveTalking && cd LiveTalking
   bash scripts/vastai/setup.sh    # cài deps + download models (~10 phút)
   bash scripts/vastai/start.sh    # production
   ```

## 🛠️ Kiến trúc

Xem chi tiết: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

```
LiveTalking/
├── app.py / config.py / registry.py
├── avatars/       # MuseTalk / Wav2Lip / Ultralight
├── tts/           # F5-TTS Vietnamese (voice cloning)
├── brain/         # Sales brain (LLM + script_engine + comments + TikTok)
├── server/        # aiohttp routes (brain/live/config/studio/webrtc)
├── studio/        # Training portal (avatar/voice/gesture jobs)
├── streamout/     # Output transports (webrtc/rtmp/virtualcam)
├── utils/         # Logger, image, device
├── web/           # SPA 3 file (vanilla JS, no framework)
├── scripts/vastai/# Deploy scripts
└── requirements.txt   # ALL deps in 1 file
```

## 📡 Output

- **WebRTC**: embed trong tab Live (default)
- **RTMP**: `--transport rtmp --push_url rtmp://your-srs/live/key` (đẩy lên TikTok / YouTube qua SRS)
- **Virtual Cam**: `--transport virtualcam` (làm camera ảo cho OBS)

## 📝 License

Apache 2.0
