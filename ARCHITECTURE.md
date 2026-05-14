# LiveTalking Sales — Kiến trúc

Digital-human livestream bán hàng tiếng Việt: avatar lip-sync + F5-TTS voice cloning + LLM sales brain + TikTok scraper, trong 1 process aiohttp.

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
│   └── f5tts.py                # F5-TTS Vietnamese (voice cloning, only TTS supported)
│
├── streamout/                  # 📡 Output transports
│   ├── base_output.py
│   ├── webrtc.py               # WebRTC peer (default for web viewer)
│   ├── rtmp.py                 # RTMP push (TikTok/YouTube/SRS)
│   └── virtualcam.py           # System virtual camera (OBS)
│
├── brain/                      # 🧠 Sales brain
│   ├── brain_manager.py        # Lifecycle: spawn LLM + script + comments
│   ├── llm_client.py           # Async OpenAI-compatible client
│   ├── script_engine.py        # 8-stage SALES_CYCLE + silence/random events
│   ├── comment_handler.py      # Intent classifier 6 cats + 5s batching
│   ├── gesture_tagger.py       # Parse [wave]/[point]/... từ LLM stream
│   ├── product_catalog.py      # Generic CRUD + hot-reload + keyword match
│   ├── live_manager.py         # Orchestrator: brain + platform listener
│   ├── platforms/
│   │   └── tiktok.py           # TikTokLive scraper (comment/like/gift/join)
│   └── prompts/
│       └── linh_vi.py          # Persona Linh (Saigon Vietnamese)
│
├── server/                     # 🌐 HTTP/WS routes (aiohttp)
│   ├── routes.py               # /human /humanaudio /set_gesture /record /interrupt_talk
│   ├── brain_routes.py         # /brain/start /stop /comment /product/switch
│   ├── live_routes.py          # /live/start /stop /state /feed (WS) — TikTok integration
│   ├── config_routes.py        # /config GET/POST — dynamic settings
│   ├── webrtc.py               # WebRTC offer/answer handler
│   ├── rtc_manager.py          # PeerConnection registry
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
├── web/                        # 🖥️ Frontend SPA (3 file, vanilla JS)
│   ├── index.html              # Admin SPA — 5 tab (Live / Sản phẩm / Video / Âm thanh / Cài đặt)
│   ├── studio.css
│   └── studio.js               # WebRTC viewer + WS feed + CRUD + dynamic config
│
├── scripts/vastai/             # 🚀 Vast.AI deploy (KHÔNG cần Docker — Vast.AI đã là container)
│   ├── setup.sh                # One-shot: deps + models download
│   ├── download_models.sh      # Idempotent model fetcher
│   ├── start.sh                # Production start (env-driven)
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

### 1. WebRTC client xem avatar
```
Browser → POST /offer → server/webrtc.py
                     → RTCManager.handle_offer
                     → session_manager.create_session()
                     → build_avatar_session() → MuseTalk/Wav2Lip load
                     → BaseAvatar.render() spawn 3 threads:
                          • asr.run_step()          (audio feature)
                          • inference()             (lip-sync diffusion)
                          • process_frames()        (paste-back + push output)
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
    → gesture_tagger.feed_stream() → (clean_sentence, {gesture})
    → avatar_session.put_msg_txt(text, {gesture})
    → F5-TTS encode → audio chunks 320 samples
    → eventpoint mang gesture → process_frames → set_gesture(name)
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
```

## Tech stack

- **Server**: Python 3.10+, aiohttp (single process)
- **GPU**: PyTorch 2.3 + CUDA 12.1
- **Avatar**: MuseTalk (Stable Diffusion based) / Wav2Lip (GAN-based) / Ultralight
- **TTS**: F5-TTS (flow matching) với fine-tune Vietnamese `hynt/F5-TTS-Vietnamese-ViVoice`
- **LLM**: OpenAI-compatible API (GPT-4o / Ollama / vLLM / Vast.AI vLLM)
- **Platform**: TikTokLive (websocket scraping)
- **Output**: aiortc (WebRTC), python_rtmpstream (RTMP), pyvirtualcam
- **Frontend**: Vanilla JS, no framework

## Deploy

Xem [scripts/vastai/setup.sh](scripts/vastai/setup.sh) và `scripts/vastai/start.sh` — Vast.AI đã là Docker container, không cần Dockerfile riêng.
