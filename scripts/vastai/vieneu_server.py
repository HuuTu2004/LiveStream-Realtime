###############################################################################
#  vieneu HTTP server — chạy trong venv_vieneu (production 3-venv setup)
#
#  Architecture (Vast.AI single instance, 3 process, 3 venv):
#
#    ┌──────────────────────────────────────────────────────────┐
#    │ venv_lmdeploy   (torch 2.4 + lmdeploy 0.6.5)              │
#    │   :23333 /v1/chat/completions                              │
#    │   VieNeu-TTS-v2 LM backbone (bfloat16 full, TurboMind GPU)│
#    └──────────────────────────────────────────────────────────┘
#                       ↓ HTTP (OpenAI-compatible chat completions)
#    ┌──────────────────────────────────────────────────────────┐
#    │ venv_vieneu     (torch 2.6 + vieneu remote + neucodec)    │
#    │   vieneu_server.py (THIS FILE)                            │
#    │   :23334 /infer_stream  (length-prefixed f32le PCM 24kHz) │
#    │   - vieneu mode='remote' → POST :23333 cho LM token gen   │
#    │   - neucodec PyTorch full → decode tokens → waveform      │
#    └──────────────────────────────────────────────────────────┘
#                       ↓ HTTP PCM stream
#    ┌──────────────────────────────────────────────────────────┐
#    │ venv_talking    (torch 2.4 + wav2lip + requests)          │
#    │   app.py + tts/vieneu_http.py                             │
#    │   :8010 / (web + wsstream)                                │
#    └──────────────────────────────────────────────────────────┘
#
#  Lợi: max quality cả backbone (bfloat16) lẫn codec (PyTorch full).
#        Zero pip/ABI conflict — mỗi venv giữ torch version riêng.
#  TTFB: ~0.26s (so với ~1s standard mode GGUF Q4).
###############################################################################

import argparse
import os
import struct
import time
import traceback

import numpy as np
from aiohttp import web


def parse_args():
    p = argparse.ArgumentParser(description="Vieneu TTS HTTP server (remote mode)")
    p.add_argument("--emotion", default=os.environ.get("VIENEU_EMOTION", "natural"),
                   choices=["natural", "storytelling"])
    p.add_argument("--port", type=int, default=int(os.environ.get("VIENEU_HTTP_PORT", "23334")),
                   help="HTTP port của vieneu_server (default 23334)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--model", default=os.environ.get("VIENEU_MODEL", "pnnbao-ump/VieNeu-TTS-v2"))
    p.add_argument("--lmdeploy_url", default=os.environ.get("LMDEPLOY_URL", "http://127.0.0.1:23333/v1"),
                   help="OpenAI-compatible api_base của lmdeploy backbone server")
    p.add_argument("--codec_repo", default=os.environ.get("VIENEU_CODEC_REPO", "neuphonic/neucodec-onnx-decoder-int8"),
                   help="HF repo neucodec. ONNX int8 = 5x faster + sạch hơn PyTorch trên VieNeu-TTS-v2 + lmdeploy.")
    return p.parse_args()


def log(msg):
    print(f"[vieneu-srv] {msg}", flush=True)


def main():
    args = parse_args()
    log(f"Loading Vieneu remote backbone={args.lmdeploy_url} model={args.model} codec={args.codec_repo}")

    # Lazy import vieneu — chỉ ở venv này (torch 2.6, không leak)
    from vieneu import Vieneu

    tts = Vieneu(
        mode="remote",
        api_base=args.lmdeploy_url,
        model_name=args.model,
        emotion=args.emotion,
        codec_repo=args.codec_repo,
    )
    voices = tts.list_preset_voices()
    log(f"Ready. {len(voices)} preset voices, sample: {[v[0] for v in voices[:5]]}")

    # ─── HTTP handlers ──────────────────────────────────────────────────
    async def health(_request):
        return web.json_response({
            "status": "ok",
            "backbone": args.lmdeploy_url,
            "codec": args.codec_repo,
            "voices": [v[0] for v in voices],
        })

    async def list_voices(_request):
        return web.json_response({"voices": [{"name": v[0], "id": v[1]} for v in voices]})

    async def infer_stream(request):
        """Stream f32le PCM 24kHz chunks. Wire format:
           Repeated: [4-byte BE length][length bytes of f32le PCM].
           Terminator: [0x00000000].
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        text = (data.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "empty text"}, status=400)
        voice_id = data.get("voice_id")
        ref_audio = data.get("ref_audio")
        ref_text = data.get("ref_text")
        voice_pkl = data.get("voice_pkl")  # pre-encoded voice (PyTorch codec encode 1 lần)

        # Author's official API defaults (xem vieneu README + API docstring):
        # temperature=1.0, top_k=50, repetition_penalty=1.2.
        # generation_config.json (0.7/20/0.8) là HF metadata không liên quan
        # tới production inference qua remote mode.
        infer_kwargs = {
            "text": text,
            "temperature": float(data.get("temperature", 1.0)),
            "top_k": int(data.get("top_k", 50)),
            "repetition_penalty": float(data.get("repetition_penalty", 1.2)),
        }
        try:
            if voice_pkl and os.path.exists(voice_pkl):
                # Pre-encoded voice (cho voice clone với ONNX codec — ONNX
                # không encode được nên cần encode trước bằng PyTorch codec
                # qua scripts/vastai/encode_voice.py).
                import pickle
                with open(voice_pkl, "rb") as f:
                    voice_data = pickle.load(f)
                infer_kwargs["ref_codes"] = voice_data["ref_codes"]
                infer_kwargs["ref_text"] = voice_data["ref_text"]
            elif voice_id:
                infer_kwargs["voice"] = tts.get_preset_voice(voice_id)
            elif ref_audio and os.path.exists(ref_audio):
                # WARN: chỉ work với PyTorch codec (encode_code).
                # ONNX codec phải dùng voice_pkl thay vì ref_audio.
                infer_kwargs["ref_audio"] = ref_audio
                if ref_text:
                    infer_kwargs["ref_text"] = ref_text
        except Exception as e:
            return web.json_response({"error": f"voice setup: {e}"}, status=400)

        resp = web.StreamResponse(headers={
            "Content-Type": "application/octet-stream",
            "X-Audio-Sample-Rate": "24000",
            "X-Audio-Format": "f32le",
        })
        await resp.prepare(request)

        # Split text thành câu nhỏ (split tại '. ! ? \n'). Mỗi câu batch
        # infer() (clean, không có streaming chunk artifact). Stream nối
        # tiếp các câu → client thấy âm thanh đến từng câu, TTFB = thời
        # gian gen câu đầu (~0.5-2s với ONNX codec 5x realtime).
        import re
        sentences = [s.strip() for s in re.split(r'(?<=[\.\!\?\n])\s+', text) if s.strip()]
        if not sentences: sentences = [text]
        log(f"split {len(sentences)} sentences")

        start = time.perf_counter()
        chunks_sent = 0
        first_sent_at = None
        try:
            for sent_idx, sentence in enumerate(sentences):
                sent_kwargs = dict(infer_kwargs)
                sent_kwargs["text"] = sentence
                t_sent = time.perf_counter()
                audio = tts.infer(**sent_kwargs)
                arr = np.asarray(audio, dtype=np.float32).reshape(-1)
                gen_time = time.perf_counter() - t_sent
                if first_sent_at is None:
                    first_sent_at = time.perf_counter() - start
                    log(f"first sentence gen @{first_sent_at:.2f}s ({len(arr)/24000:.2f}s audio) text={sentence[:30]!r}")
                # Chunk 200ms slices cho HTTP streaming response
                CHUNK_SAMPLES = 24000 // 5  # 200ms @ 24kHz
                for i in range(0, len(arr), CHUNK_SAMPLES):
                    slice_ = arr[i:i+CHUNK_SAMPLES]
                    payload = slice_.tobytes()
                    await resp.write(struct.pack(">I", len(payload)))
                    await resp.write(payload)
                    chunks_sent += 1
        except Exception as e:
            log(f"infer error: {e}\n{traceback.format_exc()}")
        try:
            await resp.write(struct.pack(">I", 0))
            await resp.write_eof()
        except Exception:
            pass
        log(f"done {chunks_sent} chunks in {time.perf_counter() - start:.2f}s")
        return resp

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/voices", list_voices)
    app.router.add_post("/infer_stream", infer_stream)

    log(f"Listening on http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=lambda _: None)


if __name__ == "__main__":
    main()
