"""ysp-live v9.5 engine package (vendored from waastudios/ysptp-docker).

The engine core (``engine.py``) is vendored unmodified apart from the EPG
import path so upstream fixes can be re-vendored by replacing that one file.
``runtime.py`` adapts it to china-live's in-process gateway.
"""

from .runtime import YspEngineError, YspRuntime

__all__ = ["YspRuntime", "YspEngineError"]