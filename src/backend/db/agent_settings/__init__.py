"""
Agent settings submodules organized by concern.

Each module provides a mixin class that AgentOperations inherits from:
- SharingMixin: Agent sharing operations
- ResourcesMixin: Memory, CPU, timeout, parallel capacity
- SecurityMixin: Full capabilities, read-only mode
- AutonomyMixin: Autonomy mode, API key settings
- AvatarMixin: Avatar identity management
- MetadataMixin: Batch queries, rename operations
"""

from .sharing import SharingMixin
from .resources import ResourcesMixin
from .security import SecurityMixin
from .autonomy import AutonomyMixin
from .avatar import AvatarMixin
from .metadata import MetadataMixin
from .access_policy import AccessPolicyMixin

__all__ = [
    'SharingMixin',
    'ResourcesMixin',
    'SecurityMixin',
    'AutonomyMixin',
    'AvatarMixin',
    'MetadataMixin',
    'AccessPolicyMixin',
]
