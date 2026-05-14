import logging
import os

# 配置日志器
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fhandler = logging.FileHandler('livetalking.log', encoding='utf-8')  # 使用 UTF-8 编码
fhandler.setFormatter(formatter)
fhandler.setLevel(logging.INFO)
logger.addHandler(fhandler)

# Optional verbose logging cho debug WebRTC ICE (set DEBUG_WEBRTC=1):
#   AIOICE/AIORTC + STUN/TURN candidate gathering, connectivity checks.
if os.environ.get('DEBUG_WEBRTC') == '1':
    for name in ('aioice.ice', 'aioice.turn', 'aioice.stun', 'aiortc', 'aiortc.rtcpeerconnection'):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        h = logging.StreamHandler()
        h.setLevel(logging.DEBUG)
        h.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(message)s'))
        lg.addHandler(h)
        lg.propagate = False