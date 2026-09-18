"""Every plain controller operation forwards its arguments unchanged.

`docs/system/rules/30-architecture.md` defines a controller as *"a flat
`<name>_controller.py` that names an operation and forwards it to one service —
no loops, no merges, no formatting, no fallbacks"*. For 215 of the layer's 252
public operations that is the entire specification, and until now nothing
checked it: the operations were executed by NiceGUI pages, and what the pages
asserted was that a screen rendered.

So this sweep asserts the specification directly. For each forwarder it patches
the one service function the controller calls, invokes the controller with a
distinct sentinel per parameter, and checks four things:

1. the service function was called **exactly once**;
2. every sentinel arrived **by identity**, in the position it was passed —
   which catches a reordered pair that an equality check would not;
3. nothing was added, dropped or defaulted on the way through;
4. the service's return value came back **unchanged**, by identity.

That is not a coverage sweep wearing a test's clothes. A controller that
reshaped an argument, swallowed a return, wrapped a result in a dict, or
acquired a fallback would fail here — and each of those is a thing this
codebase's rules forbid by name, because `history_controller` acquiring
three-source ledger merges is the recorded example of how a controller stops
being one.

**No test in this file can reach a broker, a database or a network.** The
service function is replaced before the controller is called, so the real
implementation never runs; `test_the_real_service_function_never_runs` asserts
that rather than assuming it.

The 37 operations that are NOT plain forwarders are listed in
`test_the_complex_operations_are_the_ones_we_know_about`, and each has its own
behavioural test in `test_controller_behaviour.py`. A controller that grows a
branch leaves the sweep, which fails that list until somebody justifies it.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from ._forwarders import (
    build_call, controller_function, resolve_target, scan,
)

FORWARDERS, COMPLEX = scan()

# Operations that are not `[import]; [return] alias.attr(...)`. Each carries
# real behaviour — a guard, a branch, a loop, a re-export of a constant — and
# is covered by name in test_controller_behaviour.py. The list is exact: a new
# entry means a controller grew logic, which is a decision someone has to make
# deliberately.
KNOWN_COMPLEX = [
    "auth_controller.is_debug",
    "broker_controller.ea_is_healthy",
    "broker_controller.ea_seconds_since_last_seen",
    "broker_controller.push_global_config",
    "broker_controller.push_template",
    "engines_controller.reversal_ai_apply",
    "engines_controller.reversal_ai_recommend",
    "engines_controller.reversal_research_study",
    "history_controller.broker_ts_to_local_date",
    "notifications_controller.build_orb_report",
    "remote_node_controller.restart_app",
    "sync_controller.configure",
    "sync_controller.get_remote_open_position",
    "sync_controller.is_connected",
    "sync_controller.link_state",
    "sync_controller.load_config",
    "sync_controller.note_remote_setting",
    "sync_controller.push_ai_config",
    "sync_controller.request_model_snapshot",
    "sync_controller.request_resume",
    "sync_controller.request_stand_down",
    "sync_controller.send_engine_control",
    "sync_controller.send_market_order",
    "sync_controller.server_is_running",
    "sync_controller.server_start",
    "sync_controller.server_stop",
    "sync_controller.start",
    "sync_controller.stop",
    "system_controller.app_version",
    "system_controller.local_today",
    "system_controller.releases",
    "telegram_controller.reader_is_configured",
    "trading_controller.validate_signal",
]


class _Recorder:
    """Stands in for the service function. Records the call, returns a marker.

    Not a Mock: a Mock accepts every attribute and every call shape, so a
    controller that called something else entirely would still pass. This has
    one job and an identity-checkable return.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.result = object()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _AsyncRecorder(_Recorder):
    async def __call__(self, *args, **kwargs):     # type: ignore[override]
        self.calls.append((args, kwargs))
        return self.result


def _invoke(fn, args, kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result


async def _await(awaitable):
    return await awaitable


def _binding(fn, args, kwargs) -> dict:
    """{parameter name: value} for this call, or {} if it cannot be bound.

    A builtin, a C function or a wrapper with no usable signature returns {},
    and the caller falls back to positional order.
    """
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return {}
    flat: dict = {}
    for name, value in bound.arguments.items():
        param = inspect.signature(fn).parameters[name]
        if param.kind is param.VAR_POSITIONAL:
            for i, v in enumerate(value):
                flat[f"{name}[{i}]"] = v
        elif param.kind is param.VAR_KEYWORD:
            flat.update(value)
        else:
            flat[name] = value
    return flat


def _must_return(fn, fwd) -> bool:
    """Whether this operation is obliged to hand the service's result back.

    Read from the **return annotation** wherever there is one, not from the
    function body. The body is the thing under test: deriving the expectation
    from it means a forwarder that drops its `return` also drops the assertion
    that would have caught it, and the mutation passes. That happened —
    `return _risk.get()` was changed to `_risk.get()` and this file stayed
    green until the oracle moved to the signature.

    An unannotated function falls back to the body, which is all there is. 
    """
    annotation = inspect.signature(fn).return_annotation
    if annotation is inspect.Signature.empty:
        return fwd.returns
    return annotation not in (None, "None", type(None))


def _ids(forwarders):
    return [f.qualname for f in forwarders]


# ── The sweep ────────────────────────────────────────────────────────────────

class _Injected:
    """A stand-in for an object the caller hands in, for the one forwarder that
    calls a method on its argument rather than on a service module."""

    def __init__(self, attr: str, is_async: bool) -> None:
        self.recorder = _AsyncRecorder() if is_async else _Recorder()
        setattr(self, attr, self.recorder)


def _arm(fwd, monkeypatch):
    """(recorder, real service function, args, kwargs, sentinels).

    The real function is captured BEFORE it is replaced, because its signature
    is the oracle for "did my `source` arrive as the service's `source`".
    """
    recorder = _AsyncRecorder() if fwd.is_async else _Recorder()
    fn = controller_function(fwd)
    args, kwargs, sentinels = build_call(fn)

    if fwd.on_parameter:
        injected = _Injected(fwd.attr, fwd.is_async)
        recorder = injected.recorder
        position = list(inspect.signature(fn).parameters).index(fwd.alias)
        sentinels.pop(position)
        args = args[:position] + (injected,) + args[position + 1:]
        return recorder, recorder, args, kwargs, sentinels

    target, attr = resolve_target(fwd)
    service = getattr(target, attr)
    monkeypatch.setattr(target, attr, recorder)
    return recorder, service, args, kwargs, sentinels


@pytest.mark.parametrize("fwd", FORWARDERS, ids=_ids(FORWARDERS))
def test_the_operation_forwards_its_arguments_unchanged(fwd, monkeypatch):
    attr = fwd.attr
    fn = controller_function(fwd)
    recorder, service, args, kwargs, sentinels = _arm(fwd, monkeypatch)
    returned = _invoke(fn, args, kwargs)

    assert len(recorder.calls) == 1, (
        f"{fwd.qualname} called {attr} {len(recorder.calls)} times; a controller "
        "names ONE operation and forwards it"
    )
    got_args, got_kwargs = recorder.calls[0]

    # Nothing added, nothing dropped.
    arrived = list(got_args) + list(got_kwargs.values())
    assert sorted(map(id, arrived)) == sorted(map(id, sentinels)), (
        f"{fwd.qualname} did not forward exactly its own arguments to {attr}: "
        f"sent {args}{kwargs}, forwarded {got_args}{got_kwargs}"
    )

    # And each one arrived as the SAME PARAMETER. Position alone is not the
    # test, because a forwarder may legitimately pass a positional on as a
    # keyword (`_os.kill_pid(pid, force=force)`); identity alone is not either,
    # because a swapped pair passes it — that mutation slipped through the
    # first version of this file. Binding both calls against their real
    # signatures compares them by name, which is the thing that must not
    # change.
    expected = _binding(fn, args, kwargs)
    actual = _binding(service, got_args, got_kwargs)
    shared = set(expected) & set(actual)
    if shared:
        for name in sorted(shared):
            assert actual[name] is expected[name], (
                f"{fwd.qualname} passed its {name!r} through as something else: "
                f"{attr} received {actual[name]!r} for {name!r}, not "
                f"{expected[name]!r}"
            )
    else:
        # The two sides share no parameter names (a `*args, **kwargs` forwarder
        # in front of an explicit signature, or a service the interpreter
        # cannot introspect). Order is all that is left to check.
        assert [id(v) for v in got_args] == [id(a) for a in args], (
            f"{fwd.qualname} reordered the positional arguments to {attr}"
        )

    if _must_return(fn, fwd):
        assert returned is recorder.result, (
            f"{fwd.qualname} did not return what {attr} returned. A controller "
            "that reshapes or swallows a result is doing the work the service "
            "owns."
        )


@pytest.mark.parametrize("fwd", FORWARDERS, ids=_ids(FORWARDERS))
def test_the_real_service_function_never_runs(fwd, monkeypatch):
    """The safety guard, asserted rather than assumed.

    Several of these reach order placement, the MT5 bridge or the database. The
    sweep is only safe because the service function is replaced before the
    controller is called — so this proves the replacement is what ran, by
    making the real one impossible to call without noticing.
    """
    attr = fwd.attr
    original = None
    if not fwd.on_parameter:
        target, _ = resolve_target(fwd)
        original = getattr(target, attr)

    fn = controller_function(fwd)
    recorder, _service, args, kwargs, _ = _arm(fwd, monkeypatch)

    if not fwd.on_parameter:
        target, _ = resolve_target(fwd)
        assert getattr(target, attr) is recorder, "the patch did not take"
        assert getattr(target, attr) is not original

    _invoke(fn, args, kwargs)

    assert recorder.calls, f"{fwd.qualname} did not call {attr} at all"
    # The real implementation is genuinely a different object, so the identity
    # check above is not comparing something to itself.
    assert original is not recorder


# ── The shape of the layer ───────────────────────────────────────────────────

def test_the_sweep_actually_found_the_layer():
    """Fails closed. A scan that finds nothing reports no violations and means
    nothing — the failure mode CLAUDE.md names by its incident."""
    assert len(FORWARDERS) >= 200, (
        f"only {len(FORWARDERS)} forwarders found; the controller layer has "
        "~250 operations, so the scanner has stopped reading it"
    )
    assert {f.module for f in FORWARDERS} >= {
        "trading_controller", "settings_controller", "history_controller",
        "broker_controller", "telegram_controller", "system_controller",
    }


def test_the_complex_operations_are_the_ones_we_know_about():
    """Exact, not a minimum. An operation that leaves the sweep has grown a
    branch, a loop or a fallback — the three things a controller may not have —
    and that is a decision to justify, not a quiet drop in coverage."""
    assert COMPLEX == KNOWN_COMPLEX, (
        f"new: {sorted(set(COMPLEX) - set(KNOWN_COMPLEX))}\n"
        f"gone: {sorted(set(KNOWN_COMPLEX) - set(COMPLEX))}"
    )


def test_every_forwarder_calls_exactly_one_named_service_attribute():
    """The structural half of the same claim, asserted without running
    anything: each has an alias and an attribute, and neither is empty."""
    for fwd in FORWARDERS:
        assert fwd.alias and fwd.attr, fwd.qualname


def test_the_scanner_can_tell_a_forwarder_from_a_branch(tmp_path, monkeypatch):
    """Negative control for the whole file. If `_read` classified everything as
    a forwarder, `KNOWN_COMPLEX` would be empty and the sweep would be
    exercising functions it does not understand."""
    import ast

    from . import _forwarders as mod

    src = "import svc as _svc\n"
    plain = ast.parse("def f(a):\n    return _svc.go(a)\n").body[0]
    branchy = ast.parse(
        "def g(a):\n    if a:\n        return _svc.go(a)\n    return None\n").body[0]
    looping = ast.parse(
        "def h(items):\n    for i in items:\n        _svc.go(i)\n").body[0]
    invents = ast.parse("def i(a):\n    return _svc.go(a, [])\n").body[0]
    unbound = ast.parse("def j(a):\n    return _nope.go(a)\n").body[0]

    assert mod._read(plain, src, "m") is not None
    assert mod._read(branchy, src, "m") is None
    assert mod._read(looping, src, "m") is None
    assert mod._read(invents, src, "m") is None, (
        "a call that supplies its own literal is not a pass-through"
    )
    assert mod._read(unbound, src, "m") is None, (
        "a call on a name nothing binds cannot be resolved, so it must not be "
        "reported as a forwarder"
    )
