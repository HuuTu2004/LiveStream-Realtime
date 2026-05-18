###############################################################################
#  全局会话管理器 (Session Manager)
###############################################################################

import asyncio
import uuid
from typing import Dict, Optional
from utils.logger import logger
from avatars.base_avatar import BaseAvatar

def _rand_session_id() -> str:
    """生成 UUID session ID"""
    return str(uuid.uuid4())

class SessionManager:
    """
    全局数字人会话管理器。
    
    统一管理 avatar_sessions 生命周期，并在脱离 WebRTC 时依然保持服务可用。
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized"):
            self.sessions: Dict[str, BaseAvatar] = {}
            self.build_session_fn = None
            self.max_session: Optional[int] = None  # None = không giới hạn
            self.initialized = True

    def init_builder(self, build_session_fn, max_session: Optional[int] = None):
        """配置用于构建 avatar_session 的工厂函数. max_session=None → unlimited."""
        self.build_session_fn = build_session_fn
        self.max_session = max_session if (max_session is None or max_session > 0) else None
        
    def get_session(self, sessionid: str) -> Optional[BaseAvatar]:
        """获取已存活的会话"""
        return self.sessions.get(sessionid)

    def has_session(self, sessionid: str) -> bool:
        """检查会话是否存在"""
        return sessionid in self.sessions and self.sessions[sessionid] is not None
        
    def _count_active(self) -> int:
        """Đếm session đã build xong (loại placeholder None đang loading)."""
        return sum(1 for s in self.sessions.values() if s is not None)

    async def create_session(self, params: dict, sessionid: str = None) -> str:
        """
        在异步环境中创建一个新会话
        如果 sessionid 为 None，则自动生成。
        """
        if self.build_session_fn is None:
            raise Exception("SessionManager builder not initialized")

        # Enforce hard cap — tránh OOM GPU khi caller spam create_session.
        # Count cả session '0' (default render) + placeholder đang load.
        if self.max_session is not None:
            total = len(self.sessions)
            # Cho phép update vào sessionid đã tồn tại (re-create cùng id) — không tính.
            if (sessionid is None or sessionid not in self.sessions) and total >= self.max_session:
                raise Exception(
                    f"max_session limit reached ({self.max_session}). "
                    f"Active: {total}. Gọi /remove_session để giải phóng trước."
                )

        if sessionid is None:
            sessionid = _rand_session_id()

        logger.info('Creating sessionid=%s, current session num=%d', sessionid, len(self.sessions))
        # 预先占位防止重复
        self.sessions[sessionid] = None

        # 在线程池中构建 session（加载模型非常耗时）
        avatar_session = await asyncio.get_event_loop().run_in_executor(
            None, self.build_session_fn, sessionid, params
        )
        self.sessions[sessionid] = avatar_session
        return sessionid
        
    def add_session(self, sessionid: str, avatar_session: BaseAvatar):
        """同步添加静态或外部管理的会话（供非服务端入口调用）"""
        self.sessions[sessionid] = avatar_session
        
    def remove_session(self, sessionid: str):
        """销毁会话资源 + cleanup brain + live nếu có"""
        if sessionid in self.sessions:
            logger.info(f"Removing session {sessionid}")
            self.sessions.pop(sessionid, None)
            # Live + Brain cleanup (best-effort)
            try:
                from brain.brain_manager import remove_brain
                from brain.live_manager import remove_live
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(remove_live(sessionid))
                    asyncio.ensure_future(remove_brain(sessionid))
                else:
                    loop.run_until_complete(remove_live(sessionid))
                    loop.run_until_complete(remove_brain(sessionid))
            except Exception:
                logger.exception(f"cleanup failed for {sessionid}")

# 单例抛出
session_manager = SessionManager()
