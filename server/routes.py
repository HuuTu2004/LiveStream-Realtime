###############################################################################
#  服务器路由 — 统一异常处理的 API 路由
###############################################################################

import json
import numpy as np
import asyncio
from aiohttp import web

from utils.logger import logger


# ─── 路由工具函数 ──────────────────────────────────────────────────────────

def json_ok(data=None):
    """返回成功 JSON 响应"""
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(
        content_type="application/json",
        text=json.dumps(body),
    )


def json_error(msg: str, code: int = -1):
    """返回错误 JSON 响应"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(msg)}),
    )


from server.session_manager import session_manager

def get_session(request, sessionid: str):
    """从 app 中获取 session 实例"""
    return session_manager.get_session(sessionid)


# ─── 路由处理函数 ──────────────────────────────────────────────────────────

async def human(request):
    """Text input route.

    - type='echo'  → đẩy thẳng text vào TTS queue (KHÔNG LLM).
    - type='chat'  → forward sang brain (1 LLM call, không state machine);
                     nếu brain chưa start, on-the-fly tạo LLM client.
    """
    try:
        params: dict = await request.json()

        sessionid: str = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        if params.get('interrupt'):
            avatar_session.flush_talk()

        datainfo = {}
        if params.get('tts'):
            datainfo['tts'] = params.get('tts')

        text = params.get('text', '')

        if params.get('type') == 'echo':
            avatar_session.put_msg_txt(text, datainfo)
        elif params.get('type') == 'chat':
            # Ưu tiên brain đang chạy (đã có lịch sử + product context)
            from brain.brain_manager import get_brain
            brain = get_brain(sessionid)
            opt = avatar_session.opt
            llm_key = (getattr(opt, 'llm_api_key', '') or '').strip().lower()
            if brain is not None and brain._running:
                asyncio.create_task(brain.speak(text, priority=True))
            elif llm_key in ('', 'none'):
                # Không có LLM key → fallback: gửi text thẳng vào TTS (echo).
                # Tránh tình huống browser chat → 401 → TTS silent (bug đã gặp).
                logger.info("[/human] no LLM key — fallback echo")
                avatar_session.put_msg_txt(text, datainfo)
            else:
                # Có LLM key thật → gọi LLM 1 lần (không persist history)
                from brain.llm_client import LLMClient
                from brain.gesture_tagger import GestureTagger
                client = LLMClient(
                    base_url=getattr(opt, 'llm_url', ''),
                    api_key=getattr(opt, 'llm_api_key', ''),
                    model=getattr(opt, 'llm_model', 'gpt-4o-mini'),
                )

                async def _one_shot():
                    try:
                        tagger = GestureTagger()
                        async for sent, info in tagger.feed_stream(client.stream(text, product=None)):
                            if not sent:
                                continue
                            di = dict(datainfo)
                            if info.get('gesture'):
                                di['gesture'] = info['gesture']
                            avatar_session.put_msg_txt(sent, di)
                    except Exception as e:
                        logger.warning(f"[/human] LLM stream failed: {e} — fallback echo")
                        avatar_session.put_msg_txt(text, datainfo)
                asyncio.create_task(_one_shot())

        return json_ok()
    except Exception as e:
        logger.exception('human route exception:')
        return json_error(str(e))


async def interrupt_talk(request):
    """打断当前说话"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.flush_talk()
        return json_ok()
    except Exception as e:
        logger.exception('interrupt_talk exception:')
        return json_error(str(e))


async def humanaudio(request):
    """上传音频文件"""
    try:
        form = await request.post()
        sessionid = str(form.get('sessionid', ''))
        fileobj = form["file"]
        filebytes = fileobj.file.read()

        datainfo = {}

        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.put_audio_file(filebytes, datainfo)
        return json_ok()
    except Exception as e:
        logger.exception('humanaudio exception:')
        return json_error(str(e))


async def set_audiotype(request):
    """设置自定义状态（动作编排）"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.set_custom_state(params['audiotype'])
        return json_ok()
    except Exception as e:
        logger.exception('set_audiotype exception:')
        return json_error(str(e))


async def set_gesture(request):
    """Trigger named gesture (wave/point/nod/...). Thread-safe."""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        name = params.get('name', '').strip()
        duration = params.get('duration_frames')
        ok = avatar_session.set_gesture(name, duration_frames=duration)
        if not ok:
            available = list(getattr(avatar_session, 'gesture_cycles', {}).keys())
            return json_error(f"gesture '{name}' not found. available={available}")
        return json_ok(data={'name': name})
    except Exception as e:
        logger.exception('set_gesture exception:')
        return json_error(str(e))


async def list_gestures(request):
    """Liệt kê gesture có sẵn cho 1 session."""
    try:
        sessionid = request.query.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        manifest = getattr(avatar_session, 'gesture_manifest', {})
        return json_ok(data={'gestures': manifest})
    except Exception as e:
        logger.exception('list_gestures exception:')
        return json_error(str(e))


async def record(request):
    """录制控制"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        if params['type'] == 'start_record':
            avatar_session.start_recording()
        elif params['type'] == 'end_record':
            avatar_session.stop_recording()
        return json_ok()
    except Exception as e:
        logger.exception('record exception:')
        return json_error(str(e))


async def is_speaking(request):
    """查询是否正在说话"""
    params = await request.json()
    sessionid = params.get('sessionid', '')
    avatar_session = get_session(request, sessionid)
    if avatar_session is None:
        return json_error("session not found")
    return json_ok(data=avatar_session.is_speaking())


# ─── WSStream playback (MPEG-TS over WebSocket → JSMpeg) ──────────────────

async def wsstream(request):
    """WebSocket endpoint cho transport=wsstream.

    Client (JSMpeg) connect → server đăng ký callback push MPEG-TS chunks.
    Callback chạy trên ffmpeg reader thread → dùng run_coroutine_threadsafe
    để đẩy bytes xuống WS qua asyncio loop.
    """
    sessionid = request.match_info.get('sessionid', '0')
    avatar_session = get_session(request, sessionid)
    if avatar_session is None:
        return web.Response(status=404, text=f"session {sessionid} not found")

    output = getattr(avatar_session, 'output', None)
    # Duck-type check — không import wsstream để tránh circular
    if not (output and hasattr(output, 'register_client') and hasattr(output, 'unregister_client')):
        return web.Response(
            status=400,
            text=f"session {sessionid} không dùng transport=wsstream (output={type(output).__name__})",
        )

    ws = web.WebSocketResponse(max_msg_size=0, autoping=True, heartbeat=30)
    await ws.prepare(request)
    logger.info(f"[wsstream] client connect sessionid={sessionid}")

    loop = asyncio.get_event_loop()

    def send_callback(chunk: bytes) -> None:
        """Gọi từ ffmpeg reader thread — schedule send_bytes qua asyncio loop."""
        if ws.closed:
            raise ConnectionError("ws closed")
        # Fire-and-forget — không await (sẽ block ffmpeg reader nếu await)
        asyncio.run_coroutine_threadsafe(ws.send_bytes(chunk), loop)

    output.register_client(send_callback)
    try:
        # Giữ WS mở cho tới khi client close. Không nhận gì từ client (one-way).
        async for msg in ws:
            if msg.type == web.WSMsgType.CLOSE or msg.type == web.WSMsgType.ERROR:
                break
    finally:
        output.unregister_client(send_callback)
        logger.info(f"[wsstream] client disconnect sessionid={sessionid}")
    return ws


# ─── 路由注册 ──────────────────────────────────────────────────────────────

def setup_routes(app):
    """注册所有路由到 aiohttp app"""
    app.router.add_post("/human", human)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/set_gesture", set_gesture)
    app.router.add_get("/gestures", list_gestures)
    app.router.add_post("/record", record)
    app.router.add_post("/interrupt_talk", interrupt_talk)
    app.router.add_post("/is_speaking", is_speaking)
    app.router.add_get("/wsstream/{sessionid}", wsstream)
    # NOTE: static '/' route phải đặt CUỐI cùng — không thì sẽ swallow các path khác
    # đăng ký sau (vd /studio/* ở studio.routes). Để studio_routes tự add prefix riêng.
