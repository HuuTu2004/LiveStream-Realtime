# LiveTalking Sales — Kiến trúc

Digital-human livestream bán hàng tiếng Việt: avatar lip-sync (wav2lip) +
VieNeu-TTS voice cloning + LLM sales brain + TikTok scraper.

## Deploy topology — production 3-venv (1 Vast.AI instance, 3 process)

```
┌──────────────────────────────────────────────────────────────────────┐
│  Vast.AI single GPU instance (RTX 3090/4090 24GB+)                   │
│                                                                      │
│  ┌────────────────────────────────────────────┐                      │
│  │ venv_lmdeploy   (torch 2.4 cu121)          │                      │
│  │  • lmdeploy 0.9.0 (TurboMind bfloat16)     │                      │
│  │  • transformers >=4.51 (Qwen3 arch)        │                      │
│  │                                             │                      │
│  │  lmdeploy serve api_server :23333          │                      │
│  │   • --chat-template vieneu_chat_template.json (passthrough JSON)│
│  │   • OpenAI-compat /v1/chat/completions     │                      │
│  └─────────────┬──────────────────────────────┘                      │
│                │ HTTP (text → audio_tokens "speech_xxx")             │
│                ▼                                                     │
│  ┌────────────────────────────────────────────┐                      │
│  │ venv_vieneu    (torch 2.6 cu124)           │                      │
│  │  • vieneu[gpu] (mode='remote')             │                      │
│  │  • neuphonic/neucodec-onnx-decoder-int8    │  ← ONNX, 5x realtime│
│  │                                             │                      │
│  │  scripts/vastai/vieneu_server.py :23334    │                      │
│  │   • Split text per sentence regex          │                      │
│  │   • tts.infer() batch each sentence (clean)│                      │
│  │   • Chunk 200ms HTTP response              │                      │
│  └─────────────┬──────────────────────────────┘                      │
│                │ HTTP [4-byte BE len][f32le PCM 24kHz][...][len=0]  │
│                ▼                                                     │
│  ┌────────────────────────────────────────────┐                      │
│  │ venv_talking   (torch 2.4 cu121)           │                      │
│  │  • wav2lip + aiohttp + soxr + scipy        │                      │
│  │                                             │                      │
│  │  app.py :8010                              │                      │
│  │   tts/vieneu_http.py                       │                      │
│  │    ├─ soxr 24k→16k stateful resample       │                      │
│  │    ├─ 0.5s pre-buffer absorb jitter        │                      │
│  │    └─ put_audio_frame() → asr → wav2lip   │                      │
│  │   /wsstream/{sid} → MPEG-TS over WS browser│                      │
│  └────────────────────────────────────────────┘                      │
└──────────────────────────────────────────────────────────────────────┘
```

**Tại sao 3 venv tách biệt** (sau khi thử nhiều cấu hình):
- **venv_lmdeploy** (torch 2.4): lmdeploy 0.9.0 chỉ stable với torch 2.4 cu121. Cài chung với neucodec/vieneu mới = `xgrammar/tvm_ffi/torch_c_dlpack_ext` ABI mismatch.
- **venv_vieneu** (torch 2.6): neucodec PyTorch + transformers cần torch ≥2.5. Cài chung với lmdeploy = xung đột deps.
- **venv_talking** (torch 2.4): wav2lip stable với torch 2.4. Cài chung vieneu mới = pip resolver lock.

3 venv giao tiếp qua HTTP. Zero ABI conflict. Mỗi process owns torch riêng.

## Folder layout

```
LiveTalking/
├── app.py                      # Entry point — aiohttp server + avatar load
├── config.py                   # CLI args + data/settings.json override
├── registry.py                 # Plugin registry (@register decorator)
├── requirements.txt            # ALL deps in one file
├── pyproject.toml              # Packaging metadata
├── README.md
├── ARCHITECTURE.md             # ← bạn đang đọc
│
├── avatars/                    # 🎥 Avatar models (lip-sync)
│   ├── base_avatar.py          # BaseAvatar contract + gesture system
│   ├── musetalk_avatar.py      # MuseTalk diffusion (high quality)
│   ├── wav2lip_avatar.py       # Wav2Lip (fast, pretrained)
│   ├── ultralight_avatar.py    # Ultralight (mobile-grade)
│   ├── audio_features/         # Whisper / Mel / HuBERT feature extract
│   └── musetalk/               # MuseTalk model utils (VAE/UNet)
│
├── tts/                        # 🎙️ TTS plugins
│   ├── base_tts.py             # BaseTTS contract
│   ├── vieneu_http.py          # DEFAULT — HTTP client → vieneu_server.py (production multi-venv)
│   └── vieneu.py               # Legacy in-process VieNeu lib import (single-venv, hay conflict)
│
├── streamout/                  # 📡 Output transports
│   ├── base_output.py
│   ├── wsstream.py             # MPEG-TS over WebSocket + JSMpeg (DEFAULT — ~150ms, TCP-only)
│   └── virtualcam.py           # System virtual camera (OBS local)
│
├── brain/                      # 🧠 Sales brain
│   ├── brain_manager.py        # Lifecycle: spawn LLM + script + comments
│   ├── llm_client.py           # Async OpenAI-compatible client
│   ├── script_engine.py        # 8-stage SALES_CYCLE + silence/random events
│   ├── comment_handler.py      # Intent classifier 6 cats + 5s batching
│   ├── sentence_splitter.py    # Vietnamese-aware sentence boundary splitter
│   ├── product_catalog.py      # Generic CRUD + hot-reload + keyword match
│   ├── live_manager.py         # Orchestrator: brain + platform listener
│   ├── platforms/
│   │   └── tiktok.py           # TikTokLive scraper (comment/like/gift/join)
│   └── prompts/
│       └── linh_vi.py          # Persona Linh (Saigon Vietnamese)
│
├── server/                     # 🌐 HTTP/WS routes (aiohttp)
│   ├── routes.py               # /human /humanaudio /set_gesture /record /interrupt_talk /wsstream/{sid}
│   ├── brain_routes.py         # /brain/start /stop /comment /product/switch
│   ├── live_routes.py          # /live/start /stop /state /feed (WS) — TikTok integration
│   ├── config_routes.py        # /config GET/POST — dynamic settings
│   └── session_manager.py      # Avatar session lifecycle
│
├── studio/                     # 🛠️ Training portal (avatar/voice/gesture)
│   ├── routes.py               # /studio/avatar /voice /gesture /products
│   ├── job_registry.py         # Async job queue + WS progress
│   ├── avatar_pipeline.py      # Upload → preprocess → train → preview
│   ├── voice_pipeline.py       # WAV validate + resample 24kHz
│   ├── gesture_pipeline.py     # MP4 → frames PNG via ffmpeg
│   └── workers/                # Subprocess CLI workers (JSON progress)
│       ├── preprocess_avatar.py
│       ├── train_musetalk.py
│       └── preview_avatar.py
│
├── utils/                      # Shared utilities
│   ├── logger.py
│   ├── image.py                # read_imgs, mirror_index
│   └── device.py               # CUDA/MPS init
│
├── web/                        # 🖥️ Frontend SPA (Web Components, vanilla JS, no build)
│   ├── index.html              # Shell — chỉ <app-shell>
│   ├── main.js                 # Entry — import 5 panel components
│   ├── styles/                 # tokens / base / components / layout / live / jobs
│   └── components/             # <app-shell> <live-panel> <product-panel> <video-panel> <audio-panel> <config-panel>
│       └── shared/             # api.js / toast.js / element.js (LiveElement base class)
│
├── scripts/vastai/             # 🚀 Vast.AI deploy (KHÔNG cần Docker — Vast.AI đã là container)
│   ├── setup.sh                # One-shot: tạo 2 venv (venv_talking + venv_vieneu)
│   ├── requirements_vast.txt   # venv_talking deps (wav2lip + aiohttp + scipy)
│   ├── requirements_vieneu.txt # venv_vieneu deps (vieneu[gpu] + lmdeploy)
│   ├── vieneu_server.py        # HTTP server chạy trong venv_vieneu (port 23334)
│   ├── start.sh                # Spawn vieneu_server → wait /health → launch app.py
│   ├── download_models.sh      # Idempotent model fetcher
│   └── onstart.sh              # Vast.AI On-start hook (clone + setup + start)
│
├── data/                       # 💾 Runtime data (gitignored)
│   ├── avatars/{id}/           # full_imgs/, coords.pkl, voice/, gestures/
│   ├── uploads/                # Studio job workdir
│   ├── products.json           # Catalog (CRUD qua /studio/products)
│   └── settings.json           # Dynamic config (qua /config POST)
│
├── models/                     # 🏋️ Pretrained weights (gitignored, ~12GB)
│   ├── wav2lip.pth
│   ├── musetalk/
│   ├── sd-vae-ft-mse/
│   ├── whisper/
│   └── f5tts_vi/
│
└── assets/                     # Logo, FAQ docs
```

## Request flow

### 1. WSStream client xem avatar
```
App startup → session '0' build + render thread spawn (continuous idle frames)
            → WSStreamOutput spawns ffmpeg (mpeg1video + mp2 → mpegts → stdout)
            → server reader thread cache chunks vào header_buf (ring buffer)

Browser → JSMpeg.Player('ws://host:8010/wsstream/0')
       → GET /wsstream/{sid} (WebSocket upgrade)
         → server/routes.py: register_client(send_callback)
           ├─ flush cached header chunks tới browser (PAT/PMT + recent keyframe)
           └─ ffmpeg reader thread broadcast new chunks tới send_callback
       → browser decode mpegts → canvas render + WebAudio playback

BaseAvatar.render() (already running) spawns 3 threads:
  • asr.run_step()       (audio feature)
  • inference()          (lip-sync UNet)
  • process_frames()     (paste-back + push to WSStreamOutput → ffmpeg)
```

### 2. Brain bán hàng (manual)
```
POST /brain/start {sessionid}
  → BrainManager.start()
    → ScriptEngine.start()  (asyncio loop check mỗi 5s)
    → CommentHandler.start() (batch flush mỗi 5s)

POST /brain/comment {username, text}
  → CommentHandler.on_comment()
    → classify(text) → BUY_INTENT priority hoặc batching
    → llm_client.stream(prompt, product)
    → sentence_splitter.feed_stream() → câu hoàn chỉnh
    → avatar_session.put_msg_txt(text)
    → TTS encode → audio chunks 320 samples
    → process_frames auto-trigger gesture (4-8s random) khi đang nói
```

### 3. Live livestream (TikTok auto)
```
POST /live/start {sessionid, platform:tiktok, live_id:@user}
  → LiveManager.start()
    → BrainManager.start()
    → TikTokListener.start() → TikTokLiveClient(unique_id)
      ├─ CommentEvent → brain.feed_comment
      ├─ LikeEvent → brain.on_like
      ├─ JoinEvent → brain.on_join
      ├─ FollowEvent → brain.on_follow
      ├─ ShareEvent → brain.on_share
      ├─ GiftEvent → buffer + boost engagement
      └─ RoomUserSeqEvent → brain.set_viewer_count

WS /live/feed → push state + new comments → UI render realtime
```

### 4. Dynamic config
```
POST /config {silence_gap_secs: 45, persona: linh_vi, ...}
  → save_settings_file(data/settings.json)
  → apply_overrides_to_opt(app['opt'], dynamic_fields)
  → Brain auto-restart nếu brain field thay đổi
Next start: config.py đọc settings.json override CLI defaults.
```

## Plugin pattern

```python
# brain/platforms/youtube.py (ví dụ thêm platform mới)
@register("platform", "youtube")
class YouTubeListener:
    def __init__(self, brain, live_id): ...
    async def start(self): ...

# tts/elevenlabs.py (ví dụ thêm TTS, không khuyến nghị)
@register("tts", "elevenlabs")
class ElevenLabsTTS(BaseTTS): ...
```

## State management

| Layer | Storage | Persistence | Hot-reload |
|---|---|---|---|
| CLI args | `argparse` | Process lifetime | — |
| Dynamic config | `data/settings.json` | Disk | Yes (qua `/config POST`) |
| Products | `data/products.json` | Disk | Yes (mtime check mỗi access) |
| Avatar weights | `data/avatars/{id}/` | Disk | Restart cần load lại |
| Brain state | In-memory `_brains` dict | Process lifetime | Cancel/recreate |
| Live state | In-memory `_lives` dict | Process lifetime | Cancel/recreate |
| Sessions | `session_manager.sessions` | Process lifetime | Cleanup khi WebRTC disconnect |
| Job registry | `data/uploads/jobs/{id}.json` | Disk snapshot | Replay on subscribe |

## Threading model

```
Main thread (aiohttp event loop)
  ├─ /brain/* /live/* /config/* /studio/* handlers (async)
  ├─ ScriptEngine.run() loop                       (async task)
  ├─ CommentHandler.batch_loop()                   (async task)
  └─ TikTokListener._run()                          (async task)

Per-session render thread (BaseAvatar.render)
  ├─ ASR step                                       (main thread of render)
  ├─ inference() thread  → MuseTalk/Wav2Lip UNet
  └─ process_frames() thread → paste-back + push to output

F5-TTS infer
  └─ Sync within TTS render thread (BaseTTS.process_tts) — singleton model, internal lock

VieNeu GPU mode
  ├─ Local lmdeploy subprocess (port 23333)         (spawn by plugin at __init__)
  │   └─ TurboMind engine — async batching, KV cache, FlashAttn
  └─ Plugin client (per-session)
      └─ HTTP call → 127.0.0.1:23333/v1 → response audio bytes
```

## Tech stack

- **Server**: Python 3.10+, aiohttp (single process)
- **GPU**: PyTorch 2.4+ / CUDA 12.1 (Ada/Ampere) hoặc 12.8 (Blackwell)
- **Avatar**: MuseTalk (Stable Diffusion based) / Wav2Lip (GAN-based) / Ultralight
- **TTS**: **VieNeu-TTS** `pnnbao-ump/VieNeu-TTS-v2` — Apache 2.0, voice clone 3-5s
  - turbo (default): 0.3B GGUF, CPU/GPU, không cần lmdeploy
  - standard: full GGUF + ONNX
  - gpu / remote: LMDeploy TurboMind (max throughput, cần cài thêm lmdeploy)
- **LLM**: OpenAI-compatible API (GPT-4o / Ollama / vLLM)
- **Platform**: TikTokLive (websocket scraping)
- **Output transports**:
  - `wsstream` (default): ffmpeg → mpeg1video+mp2 → mpegts → aiohttp WebSocket → JSMpeg browser. Realtime ~150ms qua TCP, bypass NAT.
  - `virtualcam`: pyvirtualcam (OBS local)
- **Frontend**: Vanilla JS Web Components, no framework, no build step

## Deploy

Xem [scripts/vastai/setup.sh](scripts/vastai/setup.sh) và `scripts/vastai/start.sh` — Vast.AI đã là Docker container, không cần Dockerfile riêng.
