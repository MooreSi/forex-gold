"""Request/event handlers -- the frontend's entire API.

Plain values in, plain values out. Nothing here imports NiceGUI.

One flat `<name>_controller.py` per page. A controller names the operations a
page can perform and forwards each to exactly one service; it does not import
`backend.src.db`, does not import a service's `repo`, and holds no loops,
merges, formatting or fallbacks. Both rules are enforced at zero -- see
`docs/system/rules/30-architecture.md` and `tools/refactor_audit/`.
"""
