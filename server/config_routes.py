"""Dynamic config — `GET /config`, `POST /config`.

Tách thành 2 nhóm field:
- `dynamic`: apply ngay vào `app['opt']`, brain auto-restart nếu đang chạy.
- `restart_required`: ghi vào `data/settings.json` nhưng cần restart server mới có hiệu lực
  (vì model nặng đã load vào GPU: avatar, transport, listenport...).

Persist: `data/settings.json` được `config.parse_args()` đọc đầu mỗi lần start để override CLI defaults.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

SETTINGS_PATH = os.environ.get("LIVETALKING_SETTINGS_PATH", "data/settings.json")


# ─── Schema ────────────────────────────────────────────────────────────
#  type:        str|int|float|bool
#  group:       UI section (tts/brain/llm/avatar/server/studio)
#  restart:     True = cần restart server; False = apply ngay
#  secret:      True = không return giá trị (chỉ trả "***") khi GET
#  choices:     dropdown options
#  description: hiển thị trên UI

CONFIG_SCHEMA: dict[str, dict] = {
    # ─── TTS / Voice ───────────────────────────────────────────────
    "tts": {"type": "str", "group": "tts", "restart": True,
            "choices": ["vieneu"],
            "description": "TTS engine: vieneu (Apache 2.0, realtime CPU/GPU)"},
    # VieNeu-TTS
    "vieneu_mode": {"type": "str", "group": "tts", "restart": True,
                    "choices": ["turbo", "standard", "gpu", "remote"],
                    "description": "turbo (DEFAULT, 0.3B 2x, no lmdeploy) | standard (GGUF+ONNX) | gpu (cần lmdeploy riêng) | remote"},
    "vieneu_emotion": {"type": "str", "group": "tts", "restart": True,
                       "choices": ["natural", "storytelling"],
                       "description": "VieNeu emotion preset"},
    "vieneu_voice_id": {"type": "str", "group": "tts", "restart": False,
                        "description": "Preset voice ID. Để trống = dùng ref_audio"},
    "vieneu_ref_audio": {"type": "str", "group": "tts", "restart": False,
                         "description": "VieNeu reference WAV (3-5s) cho voice cloning"},
    "vieneu_ref_text": {"type": "str", "group": "tts", "restart": False,
                        "description": "Transcript reference"},
    "vieneu_port": {"type": "int", "group": "tts", "restart": True,
                    "description": "(gpu mode) Local port lmdeploy (default 23333)"},
    "vieneu_tp": {"type": "int", "group": "tts", "restart": True,
                  "description": "(gpu mode) Tensor parallel size (1 GPU = 1)"},
    "vieneu_api_base": {"type": "str", "group": "tts", "restart": True,
                        "description": "(remote mode) external lmdeploy URL. Trống = auto-spawn local"},
    "vieneu_model_name": {"type": "str", "group": "tts", "restart": True,
                          "description": "HF repo cho gpu/remote mode (default pnnbao-ump/VieNeu-TTS-v2)"},

    # ─── Sales Brain ───────────────────────────────────────────────
    "brain_enabled": {"type": "bool", "group": "brain", "restart": False,
                      "description": "Bật bộ não bán hàng tự động"},
    "products_path": {"type": "str", "group": "brain", "restart": False,
                      "description": "Đường dẫn file JSON sản phẩm"},
    "persona": {"type": "str", "group": "brain", "restart": False,
                "choices": ["linh_vi"],
                "description": "Persona MC (Linh - Sài Gòn)"},
    "silence_gap_secs": {"type": "int", "group": "brain", "restart": False,
                         "description": "Số giây im lặng trước khi brain tự nói (10-120)"},

    # ─── LLM ───────────────────────────────────────────────────────
    "llm_url": {"type": "str", "group": "llm", "restart": False,
                "description": "LLM endpoint (OpenAI / Ollama / vLLM)"},
    "llm_model": {"type": "str", "group": "llm", "restart": False,
                  "description": "Tên model (gpt-4o-mini, qwen2.5:7b, ...)"},
    "llm_api_key": {"type": "str", "group": "llm", "restart": False, "secret": True,
                    "description": "API key (none nếu local)"},

    # ─── Avatar ────────────────────────────────────────────────────
    "model": {"type": "str", "group": "avatar", "restart": True,
              "choices": ["musetalk", "wav2lip", "ultralight"],
              "description": "Avatar model"},
    "avatar_id": {"type": "str", "group": "avatar", "restart": True,
                  "description": "ID avatar trong data/avatars/"},
    "batch_size": {"type": "int", "group": "avatar", "restart": True,
                   "description": "Inference batch size (8-32)"},
    "fps": {"type": "int", "group": "avatar", "restart": True,
            "description": "Output FPS (luôn 25)"},

    # ─── Server / Transport ────────────────────────────────────────
    "transport": {"type": "str", "group": "server", "restart": True,
                  "choices": ["webrtc", "rtcpush", "rtmp", "virtualcam"],
                  "description": "Output transport"},
    "push_url": {"type": "str", "group": "server", "restart": True,
                 "description": "URL push cho rtmp/rtcpush"},
    "listenport": {"type": "int", "group": "server", "restart": True,
                   "description": "HTTP port (default 8010)"},
    "max_session": {"type": "int", "group": "server", "restart": True,
                    "description": "Số session đồng thời tối đa"},

    # ─── Studio ────────────────────────────────────────────────────
    "studio_enabled": {"type": "bool", "group": "studio", "restart": True,
                       "description": "Bật cổng training studio"},
    "studio_workdir": {"type": "str", "group": "studio", "restart": True,
                       "description": "Thư mục workdir cho training jobs"},
}


def _cast(value: Any, t: str) -> Any:
    if value is None or value == "":
        if t == "bool":
            return False
        if t == "int":
            return 0
        if t == "float":
            return 0.0
        return ""
    if t == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes", "on")
    if t == "int":
        return int(value)
    if t == "float":
        return float(value)
    return str(value)


def load_settings_file(path: str = SETTINGS_PATH) -> dict:
    """Read settings.json. Trả về {} nếu không tồn tại hoặc lỗi."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        log.exception("[Config] load settings.json failed")
        return {}


def save_settings_file(data: dict, path: str = SETTINGS_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def apply_overrides_to_opt(opt, overrides: dict) -> dict:
    """Áp overrides vào opt object. Trả về dict các field thực sự đã đổi."""
    changed = {}
    for k, v in (overrides or {}).items():
        if k not in CONFIG_SCHEMA:
            continue
        casted = _cast(v, CONFIG_SCHEMA[k]["type"])
        old = getattr(opt, k, None)
        setattr(opt, k, casted)
        if old != casted:
            changed[k] = {"old": old, "new": casted}
    return changed


# ─── Route handlers ────────────────────────────────────────────────────

def _ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.json_response(body)


def _err(msg: str, code: int = -1, status: int = 200):
    return web.json_response({"code": code, "msg": str(msg)}, status=status)


async def config_get(request):
    """Trả về current config + schema metadata để frontend render form."""
    try:
        opt = request.app.get("opt")
        if opt is None:
            return _err("opt not initialized")
        current = {}
        for k, meta in CONFIG_SCHEMA.items():
            v = getattr(opt, k, None)
            if meta.get("secret") and v:
                current[k] = "***"
            else:
                current[k] = v
        saved = load_settings_file()
        return _ok({
            "current": current,
            "schema": CONFIG_SCHEMA,
            "settings_path": SETTINGS_PATH,
            "saved_keys": list(saved.keys()),
        })
    except Exception as e:
        log.exception("config_get")
        return _err(str(e))


async def config_set(request):
    """Apply config changes. Body = {field: value, ...}.

    - Dynamic field → apply vào opt ngay + persist.
    - Restart-required → chỉ persist, trả về flag restart_required.
    - Brain field thay đổi → tự restart brain nếu đang chạy.
    """
    try:
        opt = request.app.get("opt")
        if opt is None:
            return _err("opt not initialized")

        payload = await request.json()
        if not isinstance(payload, dict):
            return _err("body must be dict")

        # Filter chỉ field hợp lệ + skip masked secrets
        valid: dict = {}
        for k, v in payload.items():
            if k not in CONFIG_SCHEMA:
                continue
            # Bỏ qua nếu user gửi lại "***" cho secret (= không đổi)
            if CONFIG_SCHEMA[k].get("secret") and v == "***":
                continue
            valid[k] = v

        # Split theo dynamic vs restart
        dynamic_fields = {k: v for k, v in valid.items() if not CONFIG_SCHEMA[k]["restart"]}
        restart_fields = {k: v for k, v in valid.items() if CONFIG_SCHEMA[k]["restart"]}

        # Persist (toàn bộ) vào settings.json — merge với existing
        saved = load_settings_file()
        for k, v in valid.items():
            saved[k] = _cast(v, CONFIG_SCHEMA[k]["type"])
        save_settings_file(saved)

        # Apply dynamic ngay vào opt
        changed = apply_overrides_to_opt(opt, dynamic_fields)

        # Brain auto-reload nếu có brain field đổi và brain đang chạy
        brain_fields = {"brain_enabled", "products_path", "persona", "silence_gap_secs",
                        "llm_url", "llm_model", "llm_api_key",
                        "vieneu_ref_audio", "vieneu_ref_text"}
        brain_changed = bool(set(changed.keys()) & brain_fields)
        if brain_changed:
            try:
                from brain.brain_manager import _brains
                for sid, brain in list(_brains.items()):
                    if brain._running:
                        await brain.stop()
                        # Re-init LLMClient + script_engine + comment_handler theo opt mới
                        brain.__init__(opt, brain.avatar_session)
                        await brain.start()
                        log.info("[Config] brain %s restarted với cấu hình mới", sid)
            except Exception:
                log.exception("[Config] brain restart failed")

        return _ok({
            "applied": list(dynamic_fields.keys()),
            "restart_required": list(restart_fields.keys()),
            "changed": changed,
            "brain_restarted": brain_changed,
        })
    except Exception as e:
        log.exception("config_set")
        return _err(str(e))


async def config_schema(request):
    """Chỉ trả schema (không có current value) — dùng để render form trống."""
    return _ok({"schema": CONFIG_SCHEMA})


def setup_config_routes(app):
    app.router.add_get("/config", config_get)
    app.router.add_post("/config", config_set)
    app.router.add_get("/config/schema", config_schema)
