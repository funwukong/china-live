"""ysp-live v11.3 engine package (vendored upstream ysp-live).

``engine.py`` is vendored byte-for-byte from upstream v11.3, so upstream fixes
can be re-vendored by replacing that one file.  ``runtime.py`` adapts it to
china-live's in-process gateway and owns the EPG aggregator, which v11.3 no
longer imports itself.
"""

from .runtime import YspEngineError, YspRuntime

__all__ = ["YspRuntime", "YspEngineError"]
