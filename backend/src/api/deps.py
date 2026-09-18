"""How a router gets the live engine without importing it.

The NiceGUI pages never imported `backend.src.app` either — `frontend/app.py`
held `get_engine` and passed it down as a callable, which is why every page but
the shell stayed clean of the runtime. This module is the same arrangement with
a different shape: `server.py` calls `set_engine_provider()` once at build time,
routers depend on `engine()`.

Keeping the provider here rather than in `server.py` means a router imports
this module (inside the API layer) instead of the composition root, so there is
no import cycle and only one file in the whole layer names `backend.src.app`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

_provider: Optional[Callable[[], Any]] = None
_reader_provider: Optional[Callable[[], Any]] = None


def set_engine_provider(provider: Callable[[], Any]) -> None:
    """Called once by `server.build_app()`. Idempotent by overwrite: a second
    call replaces the provider, which is what a test fixture wants."""
    global _provider
    _provider = provider


def set_reader_provider(provider: Callable[[], Any]) -> None:
    """The Telegram reader, on the same terms as the engine.

    Separate from the engine because it is a separate handle with a separate
    lifetime: a node can have a running engine and no reader at all (no API
    credentials configured), and the Parsing tab has to say so rather than
    fail.
    """
    global _reader_provider
    _reader_provider = provider


def reader() -> Any:
    """FastAPI dependency. Returns None when this install has no reader — that
    is a state the Parsing tab renders, not an error."""
    if _reader_provider is None:
        return None
    return _reader_provider()


def clear_engine_provider() -> None:
    """For tests. A leaked provider from one test is a live runtime in the
    next one, which is exactly the kind of cross-test coupling that makes a
    suite's failures depend on ordering."""
    global _provider, _reader_provider
    _provider = None
    _reader_provider = None


def engine() -> Any:
    """FastAPI dependency. Raises rather than returning None: a router that
    receives None does not fail here, it fails four frames later inside an
    engine call with an AttributeError that names nothing useful."""
    if _provider is None:
        raise RuntimeError(
            "No engine provider registered. build_app() sets one; a test that "
            "exercises an engine-backed route must set one too."
        )
    return _provider()
