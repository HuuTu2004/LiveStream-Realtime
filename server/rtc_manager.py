###############################################################################
#  WebRTC 连接管理 + RTC 音频/视频接收
###############################################################################

import ipaddress
import json
import asyncio
import os
import random
import re
import copy
from typing import Dict, List, Optional
import queue

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceServer, RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender

from utils.logger import logger


# def _rand_session_id(n: int = 6) -> int:
#     """生成 N 位随机 session ID"""
#     return random.randint(10 ** (n - 1), 10 ** n - 1)


from server.session_manager import session_manager


_DEFAULT_STUN = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
    "stun:stun.cloudflare.com:3478",
]


def _resolve_public_ip() -> str:
    """Public IP để override host ICE candidate (Vast.ai/EC2/Docker NAT 1-1).

    Tự dò qua env: RTC_PUBLIC_IP > PUBLIC_IPADDR (Vast.ai inject sẵn).
    Trả '' nếu không có → local/host-network deploy, không cần munge.
    """
    return (
        os.environ.get('RTC_PUBLIC_IP', '') or
        os.environ.get('PUBLIC_IPADDR', '')
    ).strip()


def _build_ice_servers() -> List[RTCIceServer]:
    """STUN mặc định (Google + Cloudflare) + TURN qua env nếu có set."""
    servers: List[RTCIceServer] = [RTCIceServer(urls=_DEFAULT_STUN)]

    turn_url = os.environ.get('TURN_URL', '').strip()
    if turn_url:
        servers.append(RTCIceServer(
            urls=turn_url,
            username=os.environ.get('TURN_USER', '').strip() or None,
            credential=os.environ.get('TURN_PASS', '').strip() or None,
        ))
    return servers


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified
    )


_CANDIDATE_RE = re.compile(
    r'^(a=candidate:\S+\s+\d+\s+\S+\s+\d+\s+)(\S+)(\s+\d+\s+typ\s+)(\S+)(.*)$',
    re.MULTILINE,
)


def _munge_sdp_public_ip(sdp: str, public_ip: str) -> str:
    """Replace private IP trong SDP bằng public IP cho NAT 1-1 deployment.

    - `c=IN IP4 <private>` → `c=IN IP4 <public>`
    - `a=candidate:... <private> <port> typ host` → swap IP về public_ip
      (chỉ host candidate; srflx/prflx/relay đã có public IP đúng)
    """
    if not public_ip:
        return sdp

    def _conn_repl(m: re.Match) -> str:
        ip = m.group(2)
        if _is_private_ip(ip):
            return f'{m.group(1)}{public_ip}'
        return m.group(0)

    sdp = re.sub(r'(c=IN IP4 )(\S+)', _conn_repl, sdp)

    def _cand_repl(m: re.Match) -> str:
        head, ip, mid, typ, tail = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        if typ == 'host' and _is_private_ip(ip):
            return f'{head}{public_ip}{mid}{typ}{tail}'
        return m.group(0)

    sdp = _CANDIDATE_RE.sub(_cand_repl, sdp)
    return sdp


class RTCManager:
    """
    WebRTC 连接管理器。

    管理 PeerConnection 生命周期、音视频轨道收发、DataChannel。
    """

    def __init__(self, opt):
        """
        Args:
            opt: 全局配置
        """
        self.opt = opt
        self.pcs: set = set()
        self._ice_servers: List[RTCIceServer] = _build_ice_servers()
        self._public_ip: str = _resolve_public_ip()
        if self._public_ip:
            logger.info(f"[RTC] public_ip = {self._public_ip} (SDP host candidates sẽ được rewrite)")
        logger.info(f"[RTC] iceServers = {[s.urls for s in self._ice_servers]}")

    def ice_config_for_client(self) -> dict:
        """Trả config iceServers JSON cho frontend RTCPeerConnection."""
        out = []
        for s in self._ice_servers:
            entry = {"urls": s.urls}
            if s.username:
                entry["username"] = s.username
            if s.credential:
                entry["credential"] = s.credential
            out.append(entry)
        return {"iceServers": out}

    async def handle_offer(self, request):
        """处理 WebRTC offer 信令"""
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        if False: # 不再由 RTCManager 控制 max_session，让业务逻辑或SessionManager 控制
            logger.info('reach max session')
            return web.Response(
                content_type="application/json",
                text=json.dumps({"code": -1, "msg": "reach max session"}),
            )

        #sessionid = _rand_session_id()

        # 通过 SessionManager 构建
        sessionid = await session_manager.create_session(params)
        logger.info('offer sessionid=%s', sessionid)
        avatar_session = session_manager.get_session(sessionid)

        # 创建 PeerConnection — iceServers từ config (STUN + optional TURN)
        pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=self._ice_servers)
        )
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info("Connection state is %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                self.pcs.discard(pc)
                session_manager.remove_session(sessionid)

        # 添加发送轨道
        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)

        # 设置编解码器偏好
        capabilities = RTCRtpSender.getCapabilities("video")
        preferences = list(filter(lambda x: x.name == "H264", capabilities.codecs))
        preferences += list(filter(lambda x: x.name == "VP8", capabilities.codecs))
        preferences += list(filter(lambda x: x.name == "rtx", capabilities.codecs))
        transceiver = pc.getTransceivers()[1]
        transceiver.setCodecPreferences(preferences)

        await pc.setRemoteDescription(offer)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # NAT 1-1 fix: rewrite private host IP trong SDP answer → public IP
        # (cho Vast.ai/EC2/Docker bridge deployment, xem config.py rtc_public_ip)
        answer_sdp = _munge_sdp_public_ip(pc.localDescription.sdp, self._public_ip)

        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": answer_sdp,
                "type": pc.localDescription.type,
                "sessionid": sessionid,
            }),
        )

    async def handle_rtcpush(self, push_url, sessionid: str):
        """RTCPush 模式：主动推流"""
        import aiohttp
        await session_manager.create_session({}, sessionid)
        avatar_session = session_manager.get_session(sessionid)

        pc = RTCPeerConnection()
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info("Connection state is %s", pc.connectionState)
            if pc.connectionState == "failed":
                await pc.close()
                self.pcs.discard(pc)

        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)

        await pc.setLocalDescription(await pc.createOffer())

        async with aiohttp.ClientSession() as session:
            async with session.post(push_url, data=pc.localDescription.sdp) as response:
                answer_sdp = await response.text()

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type='answer')
        )

    async def shutdown(self):
        """关闭所有 PeerConnection"""
        coros = [pc.close() for pc in self.pcs]
        await asyncio.gather(*coros)
        self.pcs.clear()
