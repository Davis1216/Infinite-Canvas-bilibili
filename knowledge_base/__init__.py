"""Jinni knowledge-base subsystem.

The package deliberately owns persistence, indexing and retrieval so the main
application only needs to mount its router and start its worker.
"""

from .api.router import router
from .runtime import start_runtime, stop_runtime

__all__ = ["router", "start_runtime", "stop_runtime"]
