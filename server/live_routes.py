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
    """WebSocket: stream state + comment events realtime cho UI.

    Server → client (3s interval):
      {"event":"state","data":{...live.state()}}
      {"event":"comments","data":[{type,username,text,ts}, ...]}  (diff mới)
    """
    sessionid = str(request.query.get("sessionid", "0"))
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    last_comment_count = 0
    try:
        while not ws.closed:
            from brain.live_manager import get_live
            live = get_live(sessionid)
            if live is None:
                await ws.send_json({"event": "state", "data": {"running": False, "sessionid": sessionid}})
            else:
                state = live.state()
                await ws.send_json({"event": "state", "data": state})

                # Diff comments
                recent = live.recent_comments(100)
                if recent:
                    # recent đã sort newest first; lấy phần mới (vượt last_count)
                    total = state.get("platform_stats", {}).get("comments_total", 0) + \
                            state.get("platform_stats", {}).get("gifts_total", 0)
                    if total > last_comment_count:
                        new_count = total - last_comment_count
                        new_items = recent[:max(0, new_count)]
                        last_comment_count = total
                        if new_items:
                            await ws.send_json({"event": "comments", "data": new_items})

            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
            except asyncio.TimeoutError:
                pass
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception:
        log.exception("live_feed_ws")
    return ws


def setup_live_routes(app):
    app.router.add_post("/live/start", live_start)
    app.router.add_post("/live/stop", live_stop)
    app.router.add_get("/live/state", live_state)
    app.router.add_get("/live/comments", live_comments)
    app.router.add_post("/live/comment", live_manual_comment)
    app.router.add_post("/live/product/switch", live_product_switch)
    app.router.add_get("/live/feed", live_feed_ws)
