"""Brain routes — start/stop/comment/state/product/viewer + WS comment feed."""

import asyncio
import json
import logging
from aiohttp import web, WSMsgType

from server.routes import json_ok, json_error
from server.session_manager import session_manager

log = logging.getLogger(__name__)


def _get_opt(request) -> object:
    return request.app.get("opt")


def _get_session(sessionid: str):
    return session_manager.get_session(sessionid)


# ─── Brain lifecycle ───────────────────────────────────────────────────

async def brain_start(request):
    try:
        params = await request.json()
        sessionid = params.get("sessionid", "")
        avatar = _get_session(sessionid)
        if avatar is None:
            return json_error("session not found")

        opt = _get_opt(request)
        # Override một số config nếu request truyền
        if "products_path" in params and params["products_path"]:
            opt.products_path = params["products_path"]
        if "persona" in params and params["persona"]:
            opt.persona = params["persona"]
        if "silence_gap_secs" in params and params["silence_gap_secs"]:
            opt.silence_gap_secs = int(params["silence_gap_secs"])

        from brain.brain_manager import get_or_create_brain
        brain = await get_or_create_brain(opt, avatar)
        await brain.start()
        return json_ok(data={"sessionid": sessionid, "state": brain.state()})
    except Exception as e:
        log.exception("brain_start")
        return json_error(str(e))


async def brain_stop(request):
    try:
        params = await request.json()
        sessionid = params.get("sessionid", "")
        from brain.brain_manager import remove_brain
        await remove_brain(sessionid)
        return json_ok()
    except Exception as e:
        log.exception("brain_stop")
        return json_error(str(e))


async def brain_state(request):
    try:
        sessionid = request.query.get("sessionid", "")
        from brain.brain_manager import get_brain
        brain = get_brain(sessionid)
        if brain is None:
            return json_error("brain not running")
        return json_ok(data=brain.state())
    except Exception as e:
        log.exception("brain_state")
        return json_error(str(e))


# ─── Comment / interaction ─────────────────────────────────────────────

async def brain_comment(request):
    try:
        params = await request.json()
        sessionid = params.get("sessionid", "")
        from brain.brain_manager import get_brain
        brain = get_brain(sessionid)
        if brain is None:
            return json_error("brain not running")

        username = params.get("username", "bạn")
        text = params.get("text", "")
        has_icon = bool(params.get("has_icon", False))
        platform = params.get("platform", "")
        await brain.feed_comment(username, text, has_icon=has_icon, platform=platform)
        return json_ok()
    except Exception as e:
        log.exception("brain_comment")
        return json_error(str(e))


async def brain_viewer_count(request):
    try:
        params = await request.json()
        sessionid = params.get("sessionid", "")
        from brain.brain_manager import get_brain
        brain = get_brain(sessionid)
        if brain is None:
            return json_error("brain not running")
        brain.set_viewer_count(int(params.get("count", 0)))
        return json_ok()
    except Exception as e:
        log.exception("brain_viewer_count")
        return json_error(str(e))


async def brain_product_switch(request):
    try:
        params = await request.json()
        sessionid = params.get("sessionid", "")
        from brain.brain_manager import get_brain
        brain = get_brain(sessionid)
        if brain is None:
            return json_error("brain not running")
        ok = brain.switch_product(
            product_id=params.get("product_id", ""),
            index=int(params.get("index", -1)),
        )
        if not ok:
            return json_error("product not found")
        return json_ok(data=brain.state().get("current_product"))
    except Exception as e:
        log.exception("brain_product_switch")
        return json_error(str(e))


# ─── WebSocket: stream comments + state ────────────────────────────────

async def brain_comments_ws(request):
    """Bi-di WebSocket: client gửi comment JSON, server stream state events.

    Client → server:  {"action":"comment","username":"X","text":"Y", "has_icon":false, "platform":"tiktok"}
                      {"action":"viewer_count","count":120}
                      {"action":"product","product_id":"sp1"}
    Server → client:  {"event":"state","data":{...brain.state()}} (mỗi 3s)
                      {"event":"ack","action":"comment"}
    """
    sessionid = request.query.get("sessionid", "")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    from brain.brain_manager import get_brain
    brain = get_brain(sessionid)
    if brain is None:
        await ws.send_json({"event": "error", "msg": "brain not running"})
        await ws.close()
        return ws

    async def periodic_state():
        try:
            while not ws.closed:
                await asyncio.sleep(3)
                if ws.closed:
                    break
                await ws.send_json({"event": "state", "data": brain.state()})
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("brain_comments_ws periodic")

    state_task = asyncio.create_task(periodic_state())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"event": "error", "msg": "invalid json"})
                    continue
                action = data.get("action")
                if action == "comment":
                    await brain.feed_comment(
                        data.get("username", "bạn"),
                        data.get("text", ""),
                        has_icon=bool(data.get("has_icon", False)),
                        platform=data.get("platform", ""),
                    )
                    await ws.send_json({"event": "ack", "action": "comment"})
                elif action == "join":
                    await brain.on_join(data.get("username", "bạn"))
                elif action == "like":
                    await brain.on_like(data.get("username", "bạn"), int(data.get("count", 1)))
                elif action == "follow":
                    await brain.on_follow(data.get("username", "bạn"))
                elif action == "share":
                    await brain.on_share(data.get("username", "bạn"))
                elif action == "viewer_count":
                    brain.set_viewer_count(int(data.get("count", 0)))
                elif action == "product":
                    brain.switch_product(
                        product_id=data.get("product_id", ""),
                        index=int(data.get("index", -1)),
                    )
            elif msg.type == WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
    finally:
        state_task.cancel()
    return ws


# ─── Catalog mgmt ──────────────────────────────────────────────────────

async def brain_products_get(request):
    try:
        from brain.product_catalog import ProductCatalog
        opt = _get_opt(request)
        catalog = ProductCatalog.for_path(getattr(opt, "products_path", "data/products.json"))
        return json_ok(data={"products": catalog.get_all_products()})
    except Exception as e:
        log.exception("brain_products_get")
        return json_error(str(e))


# ─── Route setup ───────────────────────────────────────────────────────

def setup_brain_routes(app):
    app.router.add_post("/brain/start", brain_start)
    app.router.add_post("/brain/stop", brain_stop)
    app.router.add_post("/brain/comment", brain_comment)
    app.router.add_get("/brain/state", brain_state)
    app.router.add_post("/brain/viewer_count", brain_viewer_count)
    app.router.add_post("/brain/product/switch", brain_product_switch)
    app.router.add_get("/brain/products", brain_products_get)
    app.router.add_get("/brain/comments/ws", brain_comments_ws)
