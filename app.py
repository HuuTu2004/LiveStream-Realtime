###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

# server.py
from flask import Flask, render_template,send_from_directory,request, jsonify
from flask_sockets import Sockets
import base64
import json
#import gevent
#from gevent import pywsgi
#from geventwebsocket.handler import WebSocketHandler
import re
import numpy as np
from threading import Thread,Event
#import multiprocessing
import torch.multiprocessing as mp

from aiohttp import web
import aiohttp
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription,RTCIceServer,RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender
from server.webrtc import HumanPlayer
from avatars.base_avatar import BaseAvatar
import registry
from server.routes import setup_routes
from server.rtc_manager import RTCManager
from server.session_manager import session_manager
# NOTE: legacy llm.py đã bị xóa — /human chat hiện dùng brain.llm_client (xem server/routes.py)

import argparse
import random
import shutil
import asyncio
import torch
from io import BytesIO
from typing import Dict
from utils.logger import logger
import copy
import gc


app = Flask(__name__)
#sockets = Sockets(app)
opt = None
model = None
global_avatars = {} # avatar_id: payload
        

#####webrtc###############################
# rtc_manager replaces the old pcs set and duplicate offer handlers.
rtc_manager = None

def randN(N)->int:
    '''生成长度为 N的随机数 '''
    min = pow(10, N - 1)
    max = pow(10, N)
    return random.randint(min, max - 1)

def build_avatar_session(sessionid:str, params:dict)->BaseAvatar:
    opt_this = copy.deepcopy(opt)
    opt_this.sessionid = sessionid

    avatar_id = params.get('avatar',opt.avatar_id) 
    ref_audio = params.get('refaudio','') #音色
    ref_text = params.get('reftext','')
    if (avatar_id and avatar_id != opt.avatar_id):
        # Avoid reloading if already cached globally
        if avatar_id not in global_avatars:
            global_avatars[avatar_id] = load_avatar(avatar_id)
        avatar_this = global_avatars[avatar_id]
    else:
        # Default avatar loaded at startup
        avatar_this = global_avatars.get(opt.avatar_id)
    if ref_audio: # request override reference audio (F5-TTS voice cloning)
        opt_this.REF_FILE = ref_audio
        opt_this.REF_TEXT = ref_text
        opt_this.f5_ref_audio = ref_audio
        opt_this.f5_ref_text = ref_text
    custom_config=params.get('custom_config','') #动作编排配置
    if custom_config:
        opt_this.customopt = json.loads(custom_config)

    avatar_session = registry.create("avatar", opt.model, opt=opt_this, model=model, avatar=avatar_this)
    return avatar_session

async def offer(request):
    return await rtc_manager.handle_offer(request)

async def ice_config(request):
    """Trả iceServers cho frontend (STUN/TURN từ server config).

    Frontend fetch endpoint này trước khi `new RTCPeerConnection` để dùng đúng
    STUN/TURN deployment đã cấu hình. Tránh hard-code STUN trong HTML/JS.
    """
    return web.json_response(rtc_manager.ice_config_for_client())

async def on_shutdown(app):
    await rtc_manager.shutdown()



def main():
    global rtc_manager, opt, model,load_avatar
    # 解析命令行参数
    from config import parse_args
    opt = parse_args()

    # ─── 加载 avatar 插件（触发 @register 注册）──────────────────────
    _avatar_modules = {
        'musetalk':   'avatars.musetalk_avatar',
        'wav2lip':    'avatars.wav2lip_avatar',
        'ultralight': 'avatars.ultralight_avatar',
    }
    import importlib
    avatar_mod = importlib.import_module(_avatar_modules[opt.model])
    load_model = avatar_mod.load_model
    load_avatar = avatar_mod.load_avatar
    warm_up = avatar_mod.warm_up
    logger.info(opt)

    if opt.model == 'musetalk':
        model = load_model()
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id) 
        warm_up(opt.batch_size,model)      
    elif opt.model == 'wav2lip':
        model = load_model("./models/wav2lip.pth")
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,model,256)
    elif opt.model == 'ultralight':
        model = load_model(opt)
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,global_avatars[opt.avatar_id],160)

    # init rtc manager
    session_manager.init_builder(build_avatar_session)
    rtc_manager = RTCManager(opt)
    # share avatar_sessions (RTCManager handles it but routes.py expects it)
    
    if opt.transport=='virtualcam' or opt.transport=='rtmp':
        thread_quit = Event()
        params = {}
        # session 0 for virtualcam
        session_manager.add_session('0', build_avatar_session('0', params))
        rendthrd = Thread(target=session_manager.get_session('0').render,args=(thread_quit,))
        rendthrd.start()

    #############################################################################
    appasync = web.Application(client_max_size=1024**2*100)
    appasync["opt"] = opt

    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/offer", offer)
    appasync.router.add_get("/ice-config", ice_config)

    # 注册 server/routes.py 中的通用 API 路由
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

    # ─── Auto-start brain for non-WebRTC transports if enabled ────────
    if getattr(opt, 'brain_enabled', False) and opt.transport in ('virtualcam', 'rtmp'):
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

    # ─── Static frontend (CUỐI cùng — sau brain/studio routes) ────────
    # `/` → index.html (admin SPA)
    async def _serve_index(request):
        return web.FileResponse('web/index.html')
    appasync.router.add_get('/', _serve_index)
    appasync.router.add_static('/', path='web')

    # Configure default CORS settings.
    cors = aiohttp_cors.setup(appasync, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
    # Configure CORS on all routes.
    for route in list(appasync.router.routes()):
        try:
            cors.add(route)
        except (ValueError, RuntimeError):
            # Static resources sometimes raise — skip silently
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
        if opt.transport=='rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k!=0:
                    push_url = opt.push_url+str(k)
                loop.run_until_complete(rtc_manager.handle_rtcpush(push_url, str(k)))
        loop.run_forever()    
    #Thread(target=run_server, args=(web.AppRunner(appasync),)).start()
    run_server(web.AppRunner(appasync))

    #app.on_shutdown.append(on_shutdown)
    #app.router.add_post("/offer", offer)

    # print('start websocket server')
    # server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    # server.serve_forever()


# os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
# os.environ['MULTIPROCESSING_METHOD'] = 'forkserver'                                                    
if __name__ == '__main__':
    mp.set_start_method('spawn')
    main()
    
    
    
