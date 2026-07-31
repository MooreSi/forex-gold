"""
Remote management WebSocket protocol definitions.

All JSON messages follow: {"type": MSG_TYPE, ...extra fields}
Binary frames are used for streamed update data only.
"""

# ── Message types ──────────────────────────────────────────────────────────────

# Client → Server
MSG_HELLO          = "hello"          # first message after connect; sends token + info
MSG_PONG           = "pong"
MSG_STATUS         = "status"         # periodic heartbeat with live stats
MSG_DIAGNOSTICS    = "diagnostics"    # response to GET_DIAGNOSTICS
MSG_UPDATE_STATUS  = "update_status"  # update progress: receiving / applying / complete / failed
MSG_REGISTER       = "register"       # new client without a registered token

# Server → Client
MSG_WELCOME        = "welcome"
MSG_REJECT         = "reject"
MSG_REVOKE         = "revoke"         # admin revoked this token — client should delete local token
MSG_LICENCE        = "licence"        # admin approved: delivers the generated licence key
MSG_PING           = "ping"
MSG_GET_DIAG       = "get_diagnostics"
MSG_UPDATE_BEGIN   = "update_begin"   # precedes binary update stream
MSG_UPDATE_END     = "update_end"     # marks end of binary stream
MSG_VERSION_INFO   = "version_info"   # tells client the current latest version

# ── Privileged admin channel ───────────────────────────────────────────────────
# A trusted remote machine (identified by IOKit UUID) can open an elevated
# connection to manage registrations and licences without being co-located
# with the KeyGen directory.

# Admin client → Server
MSG_ADMIN_HELLO    = "admin_hello"    # auth: password (plain, over TLS) + machine_uuid
MSG_ADMIN_APPROVE  = "admin_approve"  # approve a pending registration
MSG_ADMIN_REJECT   = "admin_reject"   # reject / discard a pending registration
MSG_ADMIN_ISSUE    = "admin_issue"    # issue a new licence (relay to server DB)
MSG_ADMIN_REVOKE   = "admin_revoke"   # revoke an existing client token
MSG_ADMIN_DB_OP    = "admin_db_op"    # licence DB operation: revoke/reinstate/delete by id

# Server → Admin client
MSG_ADMIN_WELCOME  = "admin_welcome"  # auth accepted
MSG_PENDING_PUSH   = "pending_push"   # full list of pending registrations
MSG_CLIENTS_PUSH   = "clients_push"   # full list of known/connected clients
MSG_LICENCES_PUSH  = "licences_push"  # full list of issued licences from KeyGen DB
MSG_ADMIN_RESULT   = "admin_result"   # outcome of approve/reject/issue/revoke


def make(type_: str, **kwargs) -> dict:
    return {"type": type_, **kwargs}
