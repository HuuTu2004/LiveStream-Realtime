"""LiveTalking Sales Brain — port từ LiveAI cho livestream bán hàng tiếng Việt.

Public API: chỉ expose BrainManager. Mọi route handler nên đi qua BrainManager
để giữ encapsulation.
"""

from .brain_manager import BrainManager

__all__ = ["BrainManager"]
