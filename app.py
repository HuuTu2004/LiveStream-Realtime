###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
###############################################################################

# server.py — aiohttp app (WebRTC/RTMP stack đã loại bỏ; transport = wsstream/virtualcam)
import asyncio
import copy
import json
import random
from threading import Thread, Event
from typing import Dict

import numpy as np
import torch
import torch.multiprocessing as mp
from aiohttp import web
import aiohttp_cors

from avatars.base_avatar import BaseAvatar
import registry
from server.routes import setup_routes
from server.session_manager import session_manager
from utils.logger import logger


opt = None
model = None
global_avatars = {}   # avatar_id → payload
load_avatar = None    # gán trong main() sau khi import avatar module


def randN(N: int) -> int:
    """Random integer với N chữ số."""
    lo, hi = pow(10, N - 1), pow(10, N)
    return random.randint(lo, hi - 1)


def build_avatar_session(sessionid: str, params: dict) -> BaseAvatar:
    """Tạo avatar session với param override (avatar_id, ref_audio, custom_config)."""
    opt_this = copy.deepcopy(opt)
    opt_this.sessionid = sessionid

    avatar_id = params.get('avatar', opt.avatar_id)
    ref_audio = params.get('refaudio', '')
    ref_text  = params.get('reftext', '')
    if avatar_id and avatar_id != opt.avatar_id:
        if avatar_id not in global_avatars:
            global_avatars[avatar_id] = load_avatar(avatar_id)
        avatar_this = global_avatars[avatar_id]
    else:
        avatar_this = global_avatars.get(opt.avatar_id)

    if ref_audio:
        opt_this.REF_FILE = ref_audio
        opt_this.REF_TEXT = ref_text
        opt_this.vieneu_ref_audio = ref_audio
        opt_this.vieneu_ref_text  = ref_text

    custom_config = params.get('custom_config', '')
    if custom_config:
        opt_this.customopt = json.loads(custom_config)

    return registry.create("avatar", opt.model, opt=opt_this, model=model, avatar=avatar_this)


async def preview_info(request):
    """Trả thông tin transport hiện tại + URL preview cho frontend.

    transport=wsstream   → ws_url cho JSMpeg connect (realtime ~150ms qua TCP, no NAT issue)
    transport=virtualcam → không có URL (output ra OS virtual camera, xem qua OBS)
    """
    transport = getattr(opt, 'transport', 'wsstream')
    ws_url = ''
    if transport == 'wsstream':
        scheme = 'wss' if request.scheme == 'https' else 'ws'
        ws_url = f"{scheme}://{request.host}/wsstream/0"
    return web.json_response({
        "transport": transport,
        "ws_url":    ws_url,
    })


def main():
    global opt, model, load_avatar

    # ─── Parse args ───────────────────────────────────────────────────────
    from config import parse_args
    opt = parse_args()

    # ─── Load avatar plugin ───────────────────────────────────────────────
    _avatar_modules = {
        'musetalk':   'avatars.musetalk_avatar',
        'wav2lip':    'avatars.wav2lip_avatar',
        'ultralight': 'avatars.ultralight_avatar',
    }
    import importlib
    avatar_mod = importlib.import_module(_avatar_modules[opt.model])
    load_model = avatar_mod.load_model
    load_avatar = avatar_mod.load_avatar
    warm_up    = avatar_mod.warm_up
    logger.info(opt)

    if opt.model == 'musetalk':
        model = load_model()
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size, model)
    elif opt.model == 'wav2lip':
        model = load_model("./models/wav2lip.pth")
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size, model, 256)
    elif opt.model == 'ultralight':
        model = load_model(opt)
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size, global_avatars[opt.avatar_id], 160)

    # ─── Init session manager + render thread cho session '0' ─────────────
    session_manager.init_builder(build_avatar_session)

    # Tất cả transport hiện tại đều cần render thread chạy session '0' liên tục
    # (đẩy avatar idle/silence frames vào output kể cả khi không nói).
    thread_quit = Event()
    session_manager.add_session('0', build_avatar_session('0', {}))
    rendthrd = Thread(target=session_manager.get_session('0').render, args=(thread_quit,))
    rendthrd.start()

    # ─── aiohttp app ──────────────────────────────────────────────────────
    appasync = web.Application(client_max_size=1024 ** 2 * 100)
    appasync["opt"] = opt

    appasync.router.add_get("/preview-info", preview_info)
    setup_routes(appasync)

    # ─── Sales Brain routes (Vietnamese livestream sales) ─────────────
    from server.brain_routes import setup_brain_routes
    setup_brain_routes(appasync)

    # ─── Dynamic config routes (settings management qua web) ─────────
    from server.config_routes import setup_config_routes
    setup_config_routes(appasync)

    # ─── Live management routes (TikTok scraper + brain orchestrator) ─
    from server.live_routes import setup_live_routes
    setup_live_routes(appasync)

    # ─── Studio Portal (avatar/voice/gesture/product training UI) ─────
    if getattr(opt, 'studio_enabled', True):
        try:
            from studio.routes import setup_studio_routes
            setup_studio_routes(appasync)
            logger.info('Studio Portal enabled at /studio/*')
        except Exception:
            logger.exception('Studio Portal init failed')

    # ─── Auto-start brain ─────────────────────────────────────────────
    if getattr(opt, 'brain_enabled', False):
        async def _autostart_brain():
            try:
                from brain.brain_manager import get_or_create_brain
                avatar = session_manager.get_session('0')
                if avatar is not None:
                    brain = await get_or_create_brain(opt, avatar)
                    await brain.start()
                    logger.info('Auto-started brain for sessionid=0')
            except Exception:
                logger.exception('auto-start brain failed')
        appasync.on_startup.append(lambda app: _autostart_brain())

    # ─── Auto-resume live scrape from disk (survive app restart) ─────
    async def _autoresume_live():
        try:
            from brain.live_manager import auto_resume
            await auto_resume(opt, session_manager.get_session)
        except Exception:
            logger.exception('auto_resume live failed')
    appasync.on_startup.append(lambda app: _autoresume_live())

    # ─── Static frontend (CUỐI cùng — sau khi tất cả route động đã register) ─
    async def _serve_index(_request):
        return web.FileResponse('web/index.html')
    appasync.router.add_get('/', _serve_index)
    appasync.router.add_static('/', path='web')

    # ─── CORS ─────────────────────────────────────────────────────────
    cors = aiohttp_cors.setup(appasync, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })
    for route in list(appasync.router.routes()):
        try:
            cors.add(route)
        except (ValueError, RuntimeError):
            pass

    logger.info('=' * 60)
    logger.info(f' LiveTalking Sales — http://<serverip>:{opt.listenport}/')
    logger.info(f' Transport: {opt.transport}  |  TTS: {opt.tts}  |  Model: {opt.model}')
    logger.info(f' Brain: {"ON" if getattr(opt, "brain_enabled", False) else "OFF"}'
                f'  |  Studio: {"ON" if getattr(opt, "studio_enabled", True) else "OFF"}')
    logger.info('=' * 60)

    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '0.0.0.0', opt.listenport)
        loop.run_until_complete(site.start())
        loop.run_forever()

    run_server(web.AppRunner(appasync))


if __name__ == '__main__':
    # 'spawn' method có thể gây deadlock trên Vast container do mp.Event() cần
    # shared memory primitives. Default 'fork' trên Linux nhanh + ổn định hơn,
    # CUDA chỉ cần re-init trong child (mà mình không fork child process).
    import sys
    if sys.platform != 'win32':
        # Linux/Mac: default fork, không set
        pass
    else:
        mp.set_start_method('spawn')
    main()
