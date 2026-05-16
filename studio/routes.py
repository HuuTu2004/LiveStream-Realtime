"""Studio routes — /studio/* endpoints + WS progress stream."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil

from aiohttp import web, WSMsgType

from .job_registry import init_registry, get_registry
from . import avatar_pipeline, voice_pipeline, gesture_pipeline

log = logging.getLogger(__name__)


# ─── Util ──────────────────────────────────────────────────────────────

def _opt(request):
    return request.app.get("opt")


def _workdir(request) -> str:
    opt = _opt(request)
    wd = getattr(opt, "studio_workdir", "data/uploads") if opt else "data/uploads"
    os.makedirs(wd, exist_ok=True)
    return wd


_AVATAR_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,40}$")


def _sanitize_avatar_id(s: str) -> str:
    s = (s or "").strip()
    if not _AVATAR_ID_RE.match(s):
        raise ValueError(f"avatar_id không hợp lệ (a-z, 0-9, _, -; 2-40 ký tự): {s!r}")
    return s


def ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.json_response(body)


def err(msg: str, code: int = -1, status: int = 200):
    return web.json_response({"code": code, "msg": str(msg)}, status=status)


# ─── Avatar ────────────────────────────────────────────────────────────

async def avatar_list(request):
    try:
        return ok({"avatars": avatar_pipeline.list_avatars()})
    except Exception as e:
        log.exception("avatar_list")
        return err(str(e))


async def avatar_upload(request):
    """multipart: file (mp4), name (avatar_id)"""
    try:
        reader = await request.multipart()
        avatar_id = None
        file_bytes = None
        filename = "upload.mp4"
        async for field in reader:
            if field.name == "name":
                avatar_id = (await field.text()).strip()
            elif field.name == "file":
                filename = field.filename or "upload.mp4"
                # Stream → memory (giới hạn client_max_size đã set 100MB ở app.py)
                file_bytes = await field.read(decode=False)
        if not avatar_id or not file_bytes:
            return err("missing 'name' or 'file'")
        avatar_id = _sanitize_avatar_id(avatar_id)
        path = await avatar_pipeline.save_uploaded_video(file_bytes, filename, avatar_id, _workdir(request))
        return ok({"avatar_id": avatar_id, "raw_path": path, "size_bytes": len(file_bytes)})
    except Exception as e:
        log.exception("avatar_upload")
        return err(str(e))


async def avatar_preprocess(request):
    try:
        params = await request.json()
        avatar_id = _sanitize_avatar_id(params.get("avatar_id", ""))
        model = params.get("model", "wav2lip")
        if model not in ("musetalk", "wav2lip", "ultralight"):
            return err(f"invalid model: {model}")
        raw_dir = os.path.join(_workdir(request), "raw")
        # Tìm raw video
        candidate = None
        for ext in (".mp4", ".mov", ".mkv", ".webm"):
            p = os.path.join(raw_dir, avatar_id + ext)
            if os.path.exists(p):
                candidate = p
                break
        if not candidate:
            return err(f"chưa upload video cho avatar_id={avatar_id}")
        jid = await avatar_pipeline.preprocess(avatar_id, model, candidate, _workdir(request))
        return ok({"job_id": jid})
    except Exception as e:
        log.exception("avatar_preprocess")
        return err(str(e))


async def avatar_train(request):
    try:
        params = await request.json()
        avatar_id = _sanitize_avatar_id(params.get("avatar_id", ""))
        model = params.get("model", "musetalk")
        epochs = int(params.get("epochs", 20))
        jid = await avatar_pipeline.train(avatar_id, model, epochs, _workdir(request))
        return ok({"job_id": jid})
    except Exception as e:
        log.exception("avatar_train")
        return err(str(e))


async def avatar_delete(request):
    try:
        params = await request.json()
        avatar_id = _sanitize_avatar_id(params.get("avatar_id", ""))
        ok_flag = avatar_pipeline.delete_avatar(avatar_id)
        if not ok_flag:
            return err("avatar not found")
        return ok()
    except Exception as e:
        log.exception("avatar_delete")
        return err(str(e))


# ─── Voice ─────────────────────────────────────────────────────────────

async def voice_upload(request):
    """multipart: file (wav), text (transcript), avatar_id"""
    try:
        reader = await request.multipart()
        avatar_id = None
        transcript = ""
        wav_bytes = None
        async for field in reader:
            if field.name == "avatar_id":
                avatar_id = _sanitize_avatar_id((await field.text()).strip())
            elif field.name == "text":
                transcript = await field.text()
            elif field.name == "file":
                wav_bytes = await field.read(decode=False)
        if not avatar_id or not wav_bytes:
            return err("missing avatar_id or file")
        meta = voice_pipeline.validate_and_save(avatar_id, wav_bytes, transcript)
        return ok(meta)
    except Exception as e:
        log.exception("voice_upload")
        return err(str(e))


async def voice_delete(request):
    try:
        params = await request.json()
        avatar_id = _sanitize_avatar_id(params.get("avatar_id", ""))
        voice_pipeline.delete_voice(avatar_id)
        return ok()
    except Exception as e:
        log.exception("voice_delete")
        return err(str(e))


# ─── Gesture ───────────────────────────────────────────────────────────

async def gesture_upload(request):
    """multipart: file (mp4), name (gesture_name), avatar_id, loop?, blend?"""
    try:
        reader = await request.multipart()
        avatar_id = None
        name = None
        loop = False
        blend = 5
        mp4_bytes = None
        async for field in reader:
            if field.name == "avatar_id":
                avatar_id = _sanitize_avatar_id((await field.text()).strip())
            elif field.name == "name":
                name = (await field.text()).strip()
            elif field.name == "loop":
                loop = (await field.text()).strip().lower() in ("1", "true", "yes")
            elif field.name == "blend":
                try:
                    blend = max(1, int((await field.text()).strip()))
                except ValueError:
                    blend = 5
            elif field.name == "file":
                mp4_bytes = await field.read(decode=False)
        if not avatar_id or not name or not mp4_bytes:
            return err("missing avatar_id, name, or file")
        info = gesture_pipeline.extract_clip(avatar_id, mp4_bytes, name, loop=loop, blend=blend)
        return ok(info)
    except Exception as e:
        log.exception("gesture_upload")
        return err(str(e))


async def gesture_list(request):
    try:
        avatar_id = _sanitize_avatar_id(request.query.get("avatar_id", ""))
        manifest = gesture_pipeline.load_manifest(avatar_id)
        return ok({"gestures": manifest})
    except Exception as e:
        log.exception("gesture_list")
        return err(str(e))


async def gesture_delete(request):
    try:
        params = await request.json()
        avatar_id = _sanitize_avatar_id(params.get("avatar_id", ""))
        name = (params.get("name") or "").strip()
        if not name:
            return err("missing 'name'")
        gesture_pipeline.delete_gesture(avatar_id, name)
        return ok()
    except Exception as e:
        log.exception("gesture_delete")
        return err(str(e))


# ─── Product Catalog ───────────────────────────────────────────────────

async def products_get(request):
    try:
        opt = _opt(request)
        path = getattr(opt, "products_path", "data/products.json")
        if not os.path.exists(path):
            return ok({"products": [], "path": path})
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            products = data.get("products", [])
        else:
            products = data
        return ok({"products": products, "path": path})
    except Exception as e:
        log.exception("products_get")
        return err(str(e))


async def products_upload(request):
    """multipart: file (products.json) — bulk replace."""
    try:
        reader = await request.multipart()
        data_bytes = None
        async for field in reader:
            if field.name == "file":
                data_bytes = await field.read(decode=False)
        if not data_bytes:
            return err("missing file")
        try:
            parsed = json.loads(data_bytes.decode("utf-8"))
        except Exception as e:
            return err(f"invalid JSON: {e}")
        if isinstance(parsed, list):
            parsed = {"products": parsed}
        if not isinstance(parsed.get("products"), list):
            return err("JSON phải có key 'products' là array")

        from brain.product_catalog import ProductCatalog
        opt = _opt(request)
        path = getattr(opt, "products_path", "data/products.json")
        # Backup cũ
        if os.path.exists(path):
            shutil.copy(path, path + ".bak")
        catalog = ProductCatalog.for_path(path)
        n = catalog.replace_all(parsed["products"])
        return ok({"path": path, "count": n})
    except Exception as e:
        log.exception("products_upload")
        return err(str(e))


def _normalize_product_payload(p: dict) -> dict:
    """Schema tối giản {id, name, text}. Strip + bỏ field rỗng.

    `text` là raw description user paste vào — LLM brain nhận nguyên gốc, không
    parse trước. Phù hợp đa ngành hàng.
    """
    if not isinstance(p, dict):
        raise ValueError("payload phải là object")
    out: dict = {}
    for key in ("id", "name", "text"):
        v = p.get(key)
        if v not in (None, ""):
            out[key] = str(v).strip()
    return out


async def products_create(request):
    """POST /studio/products body = product dict."""
    try:
        opt = _opt(request)
        path = getattr(opt, "products_path", "data/products.json")
        payload = await request.json()
        product = _normalize_product_payload(payload)
        from brain.product_catalog import ProductCatalog
        catalog = ProductCatalog.for_path(path)
        created = catalog.create(product)
        return ok(created)
    except Exception as e:
        log.exception("products_create")
        return err(str(e))


async def products_update(request):
    """PUT /studio/products/{pid} body = partial product dict."""
    try:
        pid = request.match_info["pid"]
        payload = await request.json()
        fields = _normalize_product_payload(payload)
        opt = _opt(request)
        path = getattr(opt, "products_path", "data/products.json")
        from brain.product_catalog import ProductCatalog
        catalog = ProductCatalog.for_path(path)
        updated = catalog.update(pid, fields)
        return ok(updated)
    except KeyError as e:
        return err(str(e), status=404)
    except Exception as e:
        log.exception("products_update")
        return err(str(e))


async def products_delete(request):
    """DELETE /studio/products/{pid}"""
    try:
        pid = request.match_info["pid"]
        opt = _opt(request)
        path = getattr(opt, "products_path", "data/products.json")
        from brain.product_catalog import ProductCatalog
        catalog = ProductCatalog.for_path(path)
        ok_flag = catalog.delete(pid)
        if not ok_flag:
            return err("product not found", status=404)
        return ok()
    except Exception as e:
        log.exception("products_delete")
        return err(str(e))


# ─── Jobs ──────────────────────────────────────────────────────────────

async def jobs_list(request):
    try:
        return ok({"jobs": get_registry().list()})
    except Exception as e:
        return err(str(e))


async def job_get(request):
    try:
        jid = request.match_info["jid"]
        job = get_registry().get(jid)
        if not job:
            return err("job not found", status=404)
        return ok(job.to_dict())
    except Exception as e:
        return err(str(e))


async def job_cancel(request):
    try:
        jid = request.match_info["jid"]
        cancelled = get_registry().cancel(jid)
        return ok({"cancelled": cancelled})
    except Exception as e:
        return err(str(e))


async def job_ws(request):
    """WS stream — push event {update, log} cho 1 job."""
    jid = request.match_info["jid"]
    reg = get_registry()
    if reg.get(jid) is None:
        return web.Response(status=404, text="job not found")

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    q = await reg.subscribe(jid)
    try:
        while not ws.closed:
            try:
                ev = await q.get()
            except RuntimeError:
                break
            try:
                await ws.send_json(ev)
            except (ConnectionResetError, ConnectionError):
                break
            if ev.get("event") == "update":
                state = ev["data"].get("state")
                if state in ("done", "failed", "cancelled"):
                    # Stream xong, đóng sau 1 lượt flush nhỏ
                    break
    finally:
        await reg.unsubscribe(jid, q)
    return ws


# ─── Setup ─────────────────────────────────────────────────────────────

def setup_studio_routes(app):
    # Init job registry với workdir từ opt
    opt = app.get("opt")
    workdir = getattr(opt, "studio_workdir", "data/uploads") if opt else "data/uploads"
    init_registry(workdir)

    # Avatar
    app.router.add_get("/studio/avatars", avatar_list)
    app.router.add_post("/studio/avatar/upload", avatar_upload)
    app.router.add_post("/studio/avatar/preprocess", avatar_preprocess)
    app.router.add_post("/studio/avatar/train", avatar_train)
    app.router.add_post("/studio/avatar/delete", avatar_delete)

    # Voice
    app.router.add_post("/studio/voice/upload", voice_upload)
    app.router.add_post("/studio/voice/delete", voice_delete)

    # Gesture
    app.router.add_get("/studio/gestures", gesture_list)
    app.router.add_post("/studio/gesture/upload", gesture_upload)
    app.router.add_post("/studio/gesture/delete", gesture_delete)

    # Product (CRUD + bulk upload)
    app.router.add_get("/studio/products", products_get)
    app.router.add_post("/studio/products", products_create)
    app.router.add_put("/studio/products/{pid}", products_update)
    app.router.add_delete("/studio/products/{pid}", products_delete)
    app.router.add_post("/studio/products/upload", products_upload)

    # Jobs
    app.router.add_get("/studio/jobs", jobs_list)
    app.router.add_get("/studio/job/{jid}", job_get)
    app.router.add_post("/studio/job/{jid}/cancel", job_cancel)
    app.router.add_get("/studio/job/{jid}/ws", job_ws)
