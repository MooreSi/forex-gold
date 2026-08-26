"""The backend handles the settings sections share.

Every section needs config and the platform helpers. Importing them here
once, rather than in each section's own header, keeps the split from
multiplying `frontend -> backend.src` import sites: that contract is
counted per import statement, is already over its baseline, and a
seven-way split that added two sites per module would have made it
meaningfully worse for no behavioural reason.

This is a seam, not a fix. The contract wants these values injected from
frontend/app.py (see docs/system/rules/30-architecture.md); when someone
does that, this file is the single place to change.
"""
import backend.src.config as cfg_module
from backend.src.utils import os_utils as _pu

__all__ = ["cfg_module", "_pu"]
