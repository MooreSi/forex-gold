"""The HTTP layer.

The top of the stack, and the React dashboard's only way in:

    frontend/ (React, in the browser)
        │ HTTP/JSON
    backend/src/api/          ← you are here
        │ named questions
    backend/src/controllers/
        │
    backend/src/services/ → backend/src/db/

It inherits the rules the NiceGUI frontend was held to, unchanged: a router
asks a controller a named question and never reaches past it, never opens a
database connection, never writes SQL, and never decides whether an order is
allowed. The backend decides; the router forwards the answer, including the
refusal text.

Enforced by `tests/api/test_api_layer_boundary.py` and by the two contracts in
`tools/refactor_audit/import_contracts.py` that used to scan `frontend/`.

`server.py` is the one exemption: it is the composition root, it holds the
engine handle, and it injects it. Nothing else here imports `backend.src.app`.
"""
