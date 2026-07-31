"""Telegram transport: reader, outbound alerts, bot commands, logic keywords.

No broker imports. keyword_triggers closes trades only through an injected
callback; core_bot_commands_trading (which imports open_trade directly) stays
in core/ until phase 8.
"""
