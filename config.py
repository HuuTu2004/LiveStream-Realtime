###############################################################################
#  配置解析 — CLI 参数 + YAML 配置
###############################################################################

import argparse
import json
import os


def str_or_int(value):
    """尝试转换为 int，失败则返回 str"""
    try:
        return int(value)
    except ValueError:
        return value


def _str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("yes", "true", "t", "1"):
        return True
    if str(v).lower() in ("no", "false", "f", "0"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="LiveTalking Digital Human Server")

    # ─── 音频 ──────────────────────────────────────────────────────────
    parser.add_argument('--fps', type=int, default=25, help="video fps, must be 25")
    parser.add_argument('-l', type=int, default=10)
    parser.add_argument('-m', type=int, default=8)
    parser.add_argument('-r', type=int, default=10)

    # ─── 数字人模型 ────────────────────────────────────────────────────
    parser.add_argument('--model', type=str, default='wav2lip',
                        help="avatar model: musetalk/wav2lip/ultralight")
    parser.add_argument('--avatar_id', type=str, default='wav2lip256_avatar1',
                        help="avatar id in data/avatars")
    parser.add_argument('--batch_size', type=int, default=16, help="infer batch")
    parser.add_argument('--modelres', type=int, default=192)
    parser.add_argument('--modelfile', type=str, default='')

    # ─── 自定义动作和多形象 ────────────────────────────────────────────
    parser.add_argument('--customvideo_config', type=str, default='',
                        help="custom action json")

    # ─── TTS engine ────────────────────────────────────────────────────
    # `vieneu_http`  — DEFAULT, production multi-venv (LiveTalking ↔ vieneu_server.py)
    # `vieneu`       — legacy, vieneu lib in-process (cùng venv với wav2lip — dep conflict)
    parser.add_argument('--tts', type=str, default='vieneu_http',
                        help="tts plugin: vieneu_http (production HTTP client) | vieneu (in-process)")

    # Legacy refs (giữ để compat với code đọc opt.REF_FILE)
    parser.add_argument('--REF_FILE', type=str, default='',
                        help="(legacy) reference audio")
    parser.add_argument('--REF_TEXT', type=str, default='',
                        help="(legacy) reference transcript")

    # ─── VieNeu-TTS (Vietnamese, realtime, Apache 2.0) ─────────────────
    parser.add_argument('--vieneu_mode', type=str, default='turbo',
                        choices=['gpu', 'standard', 'turbo', 'turbo_gpu', 'remote'],
                        help="VieNeu mode: turbo (DEFAULT — 0.3B GGUF, llama.cpp CPU/GPU) | "
                             "turbo_gpu (transformers native GPU, NHANH NHẤT nếu có CUDA — "
                             "khuyến nghị cho RTX 4090) | "
                             "standard (full model GGUF+ONNX, chất lượng cao) | "
                             "gpu (lmdeploy TurboMind — cần cài lmdeploy riêng) | "
                             "remote (external lmdeploy server)")
    parser.add_argument('--vieneu_emotion', type=str, default='natural',
                        choices=['natural', 'storytelling'],
                        help="VieNeu emotion preset")
    parser.add_argument('--vieneu_voice_id', type=str, default='',
                        help="Preset voice ID. Để trống = dùng ref_audio")
    parser.add_argument('--vieneu_ref_audio', type=str, default='',
                        help="VieNeu reference WAV (3-5s) cho voice cloning")
    parser.add_argument('--vieneu_ref_text', type=str, default='',
                        help="Transcript của vieneu_ref_audio")
    parser.add_argument('--vieneu_api_base', type=str, default='',
                        help="(remote mode) URL lmdeploy server đã chạy sẵn. Để trống với gpu mode = auto-spawn local")
    parser.add_argument('--vieneu_model_name', type=str, default='pnnbao-ump/VieNeu-TTS-v2',
                        help="HF repo (gpu/remote mode)")
    parser.add_argument('--vieneu_port', type=int, default=23333,
                        help="(gpu mode) Local port cho lmdeploy api_server")
    parser.add_argument('--vieneu_tp', type=int, default=1,
                        help="(gpu mode) Tensor parallel size (1 GPU = 1; 2 GPU = 2)")

    # ─── vieneu_http (production multi-venv client) ──────────────────────
    # vieneu_server.py chạy trong venv_vieneu (torch 2.6+) listen 23334.
    # LiveTalking app chạy trong venv_talking (torch 2.4) → HTTP client.
    parser.add_argument('--vieneu_http_host', type=str, default='127.0.0.1',
                        help="(vieneu_http) host của vieneu_server.py")
    parser.add_argument('--vieneu_http_port', type=int, default=23334,
                        help="(vieneu_http) port của vieneu_server.py")
    parser.add_argument('--vieneu_http_timeout', type=float, default=120.0,
                        help="(vieneu_http) read timeout (s) cho mỗi sentence stream")
    parser.add_argument('--vieneu_http_prebuffer', type=float, default=0.5,
                        help="(vieneu_http) prebuffer client (s) — tăng để smoother khi jitter")
    parser.add_argument('--tts_speed', type=float, default=1.0,
                        help="TTS speed multiplier. 1.0 = normal, 1.1 = nhanh hơn 10%% "
                             "(pitch tăng nhẹ, phù hợp live bán hàng năng động).")

    # ─── 传输 ─────────────────────────────────────────────────────────
    parser.add_argument('--transport', type=str, default='wsstream',
                        choices=['wsstream', 'virtualcam'],
                        help="output transport: "
                             "wsstream = MPEG-TS over WebSocket + JSMpeg (default, "
                             "realtime ~150ms qua TCP, bypass NAT trên Vast.AI); "
                             "virtualcam = OS virtual camera cho OBS local")
    parser.add_argument('--max_session', type=int, default=1)
    parser.add_argument('--listenport', type=int, default=8010,
                        help="web listen port")

    # ─── LLM ───────────────────────────────────────────────────────────
    parser.add_argument('--llm_url', type=str, default='http://localhost:11434/v1',
                        help="LLM API server URL (Ollama/vLLM/Vast.ai)")
    parser.add_argument('--llm_model', type=str, default='qwen2.5:7b',
                        help="LLM model name")
    # Default đọc từ env OPENAI_API_KEY / LLM_API_KEY — tránh để key trong
    # `ps aux` (security). CLI arg vẫn override env nếu được pass.
    import os as _os
    _default_key = _os.environ.get('OPENAI_API_KEY') or _os.environ.get('LLM_API_KEY') or 'none'
    parser.add_argument('--llm_api_key', type=str, default=_default_key,
                        help="LLM API key (cho OpenAI/Anthropic; để 'none' cho local). "
                             "Default: lấy từ env OPENAI_API_KEY / LLM_API_KEY.")

    # ─── Sales Brain ───────────────────────────────────────────────────
    parser.add_argument('--brain_enabled', type=_str2bool, default=False,
                        help="Bật sales brain (auto-start khi tạo session)")
    parser.add_argument('--products_path', type=str, default='data/products.json',
                        help="Đường dẫn JSON catalog sản phẩm")
    parser.add_argument('--persona', type=str, default='linh_vi',
                        help="Persona prompt: linh_vi (tiếng Việt mặc định)")
    parser.add_argument('--silence_gap_secs', type=int, default=30,
                        help="(legacy) Khoảng lặng trước khi brain tự nói. Chỉ dùng khi "
                             "continuous_talk=False. Khi continuous_talk=True (default), "
                             "brain nói liên tục dựa vào idle_poll_secs.")
    parser.add_argument('--continuous_talk', type=_str2bool, default=True,
                        help="Idle-driven mode: avatar nói liên tục, vừa hết câu là fire câu kế. "
                             "False = legacy silence-driven (đợi silence_gap_secs giữa mỗi đoạn).")
    parser.add_argument('--idle_poll_secs', type=float, default=0.1,
                        help="(continuous_talk) Chu kỳ Brain check is_idle() để fire câu kế. "
                             "0.1s = phản hồi gần như tức thời khi buffer mỏng đi, CPU không "
                             "đáng kể (~10 check/s).")
    parser.add_argument('--comment_batch_secs', type=float, default=1.5,
                        help="Cửa sổ gom batch comment trước khi flush LLM. Thấp = reply nhanh "
                             "hơn nhưng nhiều LLM call hơn.")
    parser.add_argument('--random_event_chance', type=float, default=0.10,
                        help="(continuous_talk) Xác suất mỗi lượt idle fire random event "
                             "(flash_sale/stock_warning/...) thay vì stage prompt.")
    parser.add_argument('--live_active_window_secs', type=float, default=30.0,
                        help="(comment_handler) Cửa sổ tính live đang active. Có comment "
                             "trong N giây → giảm tần suất chào (3%); vắng → tăng (35%).")
    parser.add_argument('--greet_cooldown_secs', type=float, default=15.0,
                        help="(comment_handler) Cooldown global giữa các lần chào "
                             "(on_join/on_follow) để tránh spam khi spike join.")
    parser.add_argument('--target_buffer_secs', type=float, default=1.5,
                        help="(continuous_talk) Safety margin (giây audio) cần giữ trong TTS "
                             "queue NGOÀI thời gian LLM dự kiến chạy. Fire khi remaining < "
                             "(target + LLM_EMA). 1.5s = margin an toàn; threshold tổng thực "
                             "tế tự thích nghi theo LLM thực tế trong session.")
    parser.add_argument('--tts_chars_per_sec', type=float, default=14.0,
                        help="(continuous_talk) Ước tính tốc độ TTS đọc (số ký tự / giây) để "
                             "tính duration audio. Vieneu TTS tiếng Việt ~12-16 char/s. Điều "
                             "chỉnh nếu thấy buffer estimate lệch (avatar khựng / queue đầy).")
    parser.add_argument('--llm_duration_init', type=float, default=2.0,
                        help="(continuous_talk) Initial LLM duration EMA (giây). Là prior "
                             "trước khi đo được LLM thực tế. EMA α=0.3 update sau mỗi speak(), "
                             "sau ~3-4 speak là đã hội tụ về tốc độ thực.")
    parser.add_argument('--silent_sync_polls', type=int, default=5,
                        help="(continuous_talk) Số idle_poll liên tiếp thấy avatar silent thì "
                             "đồng bộ buffer estimate về now (giải kẹt khi estimate sai). "
                             "5 × 0.1s = 0.5s silent thực sự.")
    parser.add_argument('--speak_timeout_secs', type=float, default=30.0,
                        help="Hard timeout cho mỗi LLM stream. Nếu LLM treo (vLLM OOM, "
                             "network hỏng) quá timeout → cancel, release lock, brain fire "
                             "stage kế. Phòng kẹt im lặng vĩnh viễn.")

    # ─── Studio Portal ─────────────────────────────────────────────────
    parser.add_argument('--studio_enabled', type=_str2bool, default=True,
                        help="Bật cổng training studio /studio/*")
    parser.add_argument('--studio_workdir', type=str, default='data/uploads',
                        help="Thư mục lưu upload + workdir cho training jobs")

    opt = parser.parse_args()

    # ─── 后处理 ────────────────────────────────────────────────────────
    opt.customopt = []
    if opt.customvideo_config:
        with open(opt.customvideo_config, 'r') as f:
            opt.customopt = json.load(f)

    # ─── Override từ data/settings.json (dynamic config từ web) ──────
    # File này được tạo/sửa qua route POST /config. Ưu tiên thấp hơn CLI
    # explicit nhưng cao hơn default — vì argparse không biết user truyền hay default,
    # ta chỉ apply settings.json cho các key user CHƯA truyền explicit.
    settings_path = os.environ.get('LIVETALKING_SETTINGS_PATH', 'data/settings.json')
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            if isinstance(settings, dict):
                # Detect explicit CLI args (parsed != default)
                import sys
                explicit_args = set()
                for arg in sys.argv[1:]:
                    if arg.startswith('--'):
                        explicit_args.add(arg.lstrip('-').replace('-', '_'))
                for k, v in settings.items():
                    if hasattr(opt, k) and k not in explicit_args:
                        # Cast theo type của default
                        cur = getattr(opt, k)
                        try:
                            if isinstance(cur, bool):
                                v = v if isinstance(v, bool) else str(v).lower() in ('1','true','yes','on')
                            elif isinstance(cur, int) and not isinstance(cur, bool):
                                v = int(v) if v not in (None, '') else cur
                            elif isinstance(cur, float):
                                v = float(v) if v not in (None, '') else cur
                            else:
                                v = v if v is not None else cur
                        except (TypeError, ValueError):
                            continue
                        setattr(opt, k, v)
        except Exception as e:
            print(f'[config] settings.json load failed: {e}')

    return opt
