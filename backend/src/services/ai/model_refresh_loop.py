"""Daily refresh of every keyed AI provider's model catalogue.

Moved off the runtime in M4 B9e. The runtime keeps a shell that owns the
asyncio task; this owns what the task does.

`is_running` is a CALLABLE, not a bool. The flag it reads is flipped by
shutdown() while the loop is awaiting, so a captured value would leave the
loop spinning after the app was told to stop.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import asyncio
import time

from backend.src.config import is_debug as _is_debug
from backend.src.db import database as db_module
from backend.src.services.ai import provider as ai_provider


log = logging.getLogger(__name__)


async def ai_model_refresh_loop(cfg: dict, is_running: Callable[[], bool]) -> None:
    """
    Daily refresh of the Claude and DeepSeek model catalogues shown in
    Settings > AI's dropdowns. Both providers' available models change
    over time, so the dropdown is backed by a cached list (refreshed here
    and via the on-demand "Refresh Models" button) rather than a
    hardcoded array that only updates when someone edits the source.
    """
    if _is_debug():
        # Debug mode makes zero outbound requests — the cached model list
        # (or the fallback list) is fine for a simulated session.
        log.info("[debug] AI model refresh loop disabled")
        return
    await asyncio.sleep(60)  # let the app settle before the first refresh
    _INTERVAL = 86400
    while is_running():
        try:
            updates: dict = {}
            for name in ai_provider.PROVIDERS:
                key = cfg.get(ai_provider.key_field(name))
                if not key:
                    continue
                models = await ai_provider.fetch_available_models(name, key)
                updates[f"{name}_models_cache"] = models
                cfg[f"{name}_models_cache"] = models
            if updates:
                updates["ai_models_last_refreshed"] = time.time()
                cfg["ai_models_last_refreshed"] = updates["ai_models_last_refreshed"]
                import backend.src.config as _cfg_mod
                await db_module.to_db_thread(lambda: _cfg_mod.save_to_yaml(updates))
                log.info("[AI] model catalogue refreshed: %s", list(updates.keys()))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug("_ai_model_refresh_loop error: %s", e)
        await asyncio.sleep(_INTERVAL)
