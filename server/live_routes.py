"""Live management routes — quản lý 1 phiên livestream end-to-end.

Endpoints:
  POST /live/start    {sessionid, platform, live_id}   → start brain + TikTok scraper
  POST /live/stop     {sessionid}                       → stop all
  GET  /live/state    ?sessionid=...                    → snapshot state
  GET  /live/comments ?sessionid=...&limit=50          → recent comments
  WS   /live/feed     ?sessionid=...                    → realtime stream state + comment
"""

from __future__ import annotations

import asyncio
import json
import logging
from aiohttp import web, WSMsgType

from server.session_manager import session_manager

log = logging.getLogger(__name__)


def _ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.json_response(body)


def _err(msg: str, status: int = 200):
    return web.json_response({"code": -1, "msg": str(msg)}, status=status)


# ─── Handlers ──────────────────────────────────────────────────────────

async def live_start(request):
    try:
        params = await request.json()
        sessionid = str(params.get("sessionid", "0"))
        platform = (params.get("platform") or "tiktok").strip().lower()
        live_id = (params.get("live_id") or "").strip()

        if not live_id:
            return _err("Thiếu live_id (vd: @username TikTok)")

        avatar = session_manager.get_session(sessionid)
        if avatar is None:
            return _err(f"session '{sessionid}' không tồn tại — connect WebRTC trước (hoặc dùng sessionid='0' với rtmp/virtualcam)")

        opt = request.app.get("opt")
        from brain.live_manager import get_or_create_live
        live = await get_or_create_live(opt, avatar)
        result = await live.start(platform, live_id)
        if "error" in result:
            return _err(result["error"])
        return _ok(result["state"])
    except Exception as e:
        log.exception("live_start")
        return _err(str(e))


async def live_stop(request):
    try:
        params = await request.json()
        sessionid = str(params.get("sessionid", "0"))
        from brain.live_manager import get_live
        live = get_live(sessionid)
        if live is None:
            return _ok({"running": False})
        result = await live.stop()
        return _ok(result["state"])
    except Exception as e:
        log.exception("live_stop")
        return _err(str(e))


async def live_state(request):
    try:
        sessionid = str(request.query.get("sessionid", "0"))
        from brain.live_manager import get_live
        live = get_live(sessionid)
        if live is None:
            return _ok({"running": False, "sessionid": sessionid})
        return _ok(live.state())
    except Exception as e:
        log.exception("live_state")
        return _err(str(e))


async def live_comments(request):
    try:
        sessionid = str(request.query.get("sessionid", "0"))
        limit = int(request.query.get("limit", "50"))
        from brain.live_manager import get_live
        live = get_live(sessionid)
        if live is None:
            return _ok({"comments": []})
        return _ok({"comments": live.recent_comments(limit)})
    except Exception as e:
        log.exception("live_comments")
        return _err(str(e))


async def live_manual_comment(request):
    """Gửi comment thủ công vào brain (test, hoặc khi không kết nối được platform)."""
    try:
        params = await request.json()
        sessionid = str(params.get("sessionid", "0"))
        username = params.get("username", "bạn")
        text = params.get("text", "")
        from brain.live_manager import get_live
        live = get_live(sessionid)
        if live is None:
            return _err("live chưa start")
        await live.feed_comment_manual(username, text)
        return _ok()
    except Exception as e:
        log.exception("live_manual_comment")
        return _err(str(e))


async def live_product_switch(request):
    try:
        params = await request.json()
        sessionid = str(params.get("sessionid", "0"))
        from brain.live_manager import get_live
        live = get_live(sessionid)
        if live is None:
            return _err("live chưa start")
        ok_flag = live.switch_product(
            product_id=params.get("product_id", ""),
            index=int(params.get("index", -1)),
        )
        if not ok_flag:
            return _err("product không tồn tại")
        return _ok(live.state())
    except Exception as e:
        log.exception("live_product_switch")
        return _err(str(e))


async def live_feed_ws(request):
    """Realtime WebSocket: subscribe vào live event bus, push trực tiếp tới UI.

    Server → client:
      {"event":"state",    "data":{...live.state()}}            # 1s snapshot
      {"event":"comments", "data":[{type,username,text,ts},...]}# push as-it-happens
      {"event":"stat",     "data":{...platform_stats}}          # KPI delta push

    State snapshot mỗi 1s vừa là catch-up cho client mới connect, vừa đồng bộ
    `running` / `current_product` / `stage`. Comments/stats không đợi tick này
    — listener publish ngay khi event tới.
    """
    sessionid = str(request.query.get("sessionid", "0"))
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    from brain.live_manager import get_live

    # Shared mutable subscription state. The state loop may subscribe lazily
    # once a LiveManager appears (user clicks Start Live after WS opens).
    sub = {"live": None, "queue": None}

    async def _attach_if_ready():
        if sub["live"] is not None:
            return
        live2 = get_live(sessionid)
        if live2 is None:
            return
        q = live2.subscribe()
        sub["live"] = live2
        sub["queue"] = q
        # Wake the event loop so it starts draining the new queue.
        nonlocal_event.set()
        # Send catch-up snapshot + buffered comments.
        try:
            await ws.send_json({"event": "state", "data": live2.state()})
            recent = live2.recent_comments(50)
            if recent:
                await ws.send_json({"event": "comments", "data": list(reversed(recent))})
        except Exception:
            log.exception("live_feed_ws catch-up send")

    nonlocal_event = asyncio.Event()

    # Initial state — may be None pre-start; the state loop will subscribe
    # later if/when the LiveManager appears.
    try:
        live0 = get_live(sessionid)
        if live0 is None:
            await ws.send_json({"event": "state", "data": {"running": False, "sessionid": sessionid}})
        else:
            await _attach_if_ready()
    except Exception:
        log.exception("live_feed_ws initial send")

    async def push_state_loop():
        """1s snapshot ticker. Also lazily attaches subscription if the
        LiveManager appears mid-session, and detects when it disappears
        (e.g. user clicked Stop Live)."""
        try:
            while not ws.closed:
                await asyncio.sleep(1.0)
                if sub["live"] is None:
                    await _attach_if_ready()
                live2 = sub["live"] or get_live(sessionid)
                if live2 is None:
                    try:
                        await ws.send_json({"event": "state", "data": {"running": False, "sessionid": sessionid}})
                    except (ConnectionResetError, RuntimeError):
                        break
                    continue
                try:
                    await ws.send_json({"event": "state", "data": live2.state()})
                except (ConnectionResetError, RuntimeError):
                    break
        except asyncio.CancelledError:
            pass

    async def push_event_loop():
        """Drain whichever subscription queue is currently active."""
        try:
            while not ws.closed:
                if sub["queue"] is None:
                    await nonlocal_event.wait()
                    nonlocal_event.clear()
                    continue
                try:
                    event = await sub["queue"].get()
                except (asyncio.CancelledError, RuntimeError):
                    break
                try:
                    await ws.send_json(event)
                except (ConnectionResetError, RuntimeError):
                    break
        except asyncio.CancelledError:
            pass

    async def receive_loop():
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    state_task = asyncio.create_task(push_state_loop())
    event_task = asyncio.create_task(push_event_loop())
    try:
        await receive_loop()
    finally:
        state_task.cancel()
        event_task.cancel()
        for t in (state_task, event_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if sub["queue"] is not None and sub["live"] is not None:
            sub["live"].unsubscribe(sub["queue"])
    return ws


def setup_live_routes(app):
    app.router.add_post("/live/start", live_start)
    app.router.add_post("/live/stop", live_stop)
    app.router.add_get("/live/state", live_state)
    app.router.add_get("/live/comments", live_comments)
    app.router.add_post("/live/comment", live_manual_comment)
    app.router.add_post("/live/product/switch", live_product_switch)
    app.router.add_get("/live/feed", live_feed_ws)
