"""Signal ingestion: parse, classify, store, resolve.

Produces and resolves signal rows; never places, modifies or closes an order.
bridge use here is read-only (get_tick). The modules that DO touch orders --
update_signal, instant_entry, instant_followup, scan_messages_auto_execute --
stay in core/ until the trading phases, verified per module.
"""
