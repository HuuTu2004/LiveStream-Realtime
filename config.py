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
    # `vieneu` — Apache 2.0, realtime CPU/GPU, voice clone 3-5s ref.
    parser.add_argument('--tts', type=str, default='vieneu',
                        help="tts plugin: vieneu")

    # Legacy refs (giữ để compat với code đọc opt.REF_FILE)
    parser.add_argument('--REF_FILE', type=str, default='',
                        help="(legacy) reference audio")
    parser.add_argument('--REF_TEXT', type=str, default='',
                        help="(legacy) reference transcript")

    # ─── VieNeu-TTS (Vietnamese, realtime, Apache 2.0) ─────────────────
    parser.add_argument('--vieneu_mode', type=str, default='gpu',
                        choices=['gpu', 'standard', 'turbo', 'remote'],
                        help="VieNeu mode: gpu (DEFAULT — tự spawn lmdeploy local, max GPU perf) | "
                             "standard (GGUF+ONNX local) | turbo (0.3B 2x faster) | remote (external lmdeploy)")
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

    # ─── 传输 ─────────────────────────────────────────────────────────
    parser.add_argument('--transport', type=str, default='webrtc',
                        help="output: rtcpush/webrtc/rtmp/virtualcam")
    parser.add_argument('--push_url', type=str,
                        default='http://localhost:1985/rtc/v1/whip/?app=live&stream=livestream')
    parser.add_argument('--max_session', type=int, default=1)
    parser.add_argument('--listenport', type=int, default=8010,
                        help="web listen port")
    # WebRTC NAT/ICE tự dò qua env vars khi cần: PUBLIC_IPADDR (Vast.ai inject
    # sẵn) hoặc RTC_PUBLIC_IP; TURN_URL/TURN_USER/TURN_PASS nếu cần TURN.
    # Local dev: không cần set gì cả. Xem server/rtc_manager.py.

    # ─── LLM ───────────────────────────────────────────────────────────
    parser.add_argument('--llm_url', type=str, default='http://localhost:11434/v1',
                        help="LLM API server URL (Ollama/vLLM/Vast.ai)")
    parser.add_argument('--llm_model', type=str, default='qwen2.5:7b',
                        help="LLM model name")
    parser.add_argument('--llm_api_key', type=str, default='none',
                        help="LLM API key (cho OpenAI/Anthropic; để 'none' cho local)")

    # ─── Sales Brain ───────────────────────────────────────────────────
    parser.add_argument('--brain_enabled', type=_str2bool, default=False,
                        help="Bật sales brain (auto-start khi tạo session)")
    parser.add_argument('--products_path', type=str, default='data/products.json',
                        help="Đường dẫn JSON catalog sản phẩm")
    parser.add_argument('--persona', type=str, default='linh_vi',
                        help="Persona prompt: linh_vi (tiếng Việt mặc định)")
    parser.add_argument('--silence_gap_secs', type=int, default=30,
                        help="Khoảng lặng trước khi brain tự nói (giây)")

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
