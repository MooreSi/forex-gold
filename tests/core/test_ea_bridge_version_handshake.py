"""EABridge's EA version handshake -- the "hello" ea_version/compiled fields
checked against the repo's own mql5/ForexTraderBridge.mq5, so a terminal
running a stale .ex5 announces itself instead of silently managing trades
with month-old rules (see the note at the top of tools/deploy_ea.sh for the
failure this exists to catch).

Kept in lockstep with EnsureConnected() in ForexTraderBridge.mq5, which
builds the hello payload these tests stand in for."""
import datetime
import logging

from backend.src.services.broker import ea_bridge


def _bridge():
    return ea_bridge.EABridge(engine=None)


def _stamp(offset_minutes: float = 0.0) -> str:
    """A compile stamp `offset_minutes` from the EA source's own mtime.

    Anchored to the source, not to now: the drift check compares the two, and
    a checkout that has sat untouched for a week would otherwise make
    "compiled 3 hours ago" test as *newer* than the source and quietly stop
    exercising the branch it names.
    """
    base = datetime.datetime.fromtimestamp(ea_bridge._EA_SOURCE.stat().st_mtime)
    return (base + datetime.timedelta(minutes=offset_minutes)).strftime(
        ea_bridge._EA_COMPILED_FMT)


def test_ea_source_declares_a_parseable_version():
    """The #define the EA sends and the one Python reads are the same literal
    in the same file; if the EA is ever restructured so this regex misses,
    every connection silently degrades to the "no source to check" path."""
    assert ea_bridge._expected_ea_version() is not None


def test_matching_version_and_fresh_build_is_ok(caplog):
    bridge = _bridge()
    with caplog.at_level(logging.WARNING, logger="ea_bridge"):
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(),
        })
    assert bridge.ea_version_ok is True
    assert caplog.records == []


def test_version_mismatch_warns_and_is_not_ok(caplog):
    bridge = _bridge()
    with caplog.at_level(logging.WARNING, logger="ea_bridge"):
        bridge._check_ea_version({"ea_version": "0.01", "compiled": _stamp()})
    assert bridge.ea_version_ok is False
    assert "MISMATCH" in caplog.text
    # The reported version is kept even when wrong -- it's what a UI or a log
    # reader needs to know which build is actually out there.
    assert bridge.ea_version == "0.01"


def test_ea_predating_the_handshake_is_not_ok(caplog):
    """An old .ex5 connects and works, but sends no ea_version at all. That
    absence is itself proof of staleness and must not be treated as 'fine'."""
    bridge = _bridge()
    with caplog.at_level(logging.WARNING, logger="ea_bridge"):
        bridge._check_ea_version({"account": 1, "symbol": "XAUUSD"})
    assert bridge.ea_version_ok is False
    assert bridge.ea_version is None
    assert "no version" in caplog.text


def test_source_newer_than_build_warns_but_stays_ok(caplog):
    """Matching versions with a build older than the source means edits were
    made without bumping EA_VERSION. Advisory only: it fires on edits that
    never reached a terminal, so it warns without flipping ea_version_ok."""
    bridge = _bridge()
    with caplog.at_level(logging.WARNING, logger="ea_bridge"):
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(offset_minutes=-180),
        })
    assert bridge.ea_version_ok is True
    assert "does not have" in caplog.text


def test_small_save_after_compile_is_not_flagged(caplog):
    """Saving the .mq5 a moment after the compile that read it is normal and
    must not warn, or the warning becomes noise and stops being read."""
    bridge = _bridge()
    with caplog.at_level(logging.WARNING, logger="ea_bridge"):
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(offset_minutes=-0.5),
        })
    assert caplog.records == []


def test_unparseable_compile_stamp_does_not_break_the_check(caplog):
    """A malformed timestamp from an EA build with a different format must
    not cost us the version comparison, which is the part that matters."""
    bridge = _bridge()
    with caplog.at_level(logging.WARNING, logger="ea_bridge"):
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": "not-a-timestamp",
        })
    assert bridge.ea_version_ok is True


class TestTheDriftIsRECORDED_notJustLogged:
    """A warning nobody reads is how this failed the first time.

    The version comparison has a badge; the drift check never did. It only
    ever logged, so an EA whose version matches but whose source was edited
    after the last compile showed a GREEN badge while running rules the app
    no longer ships. Seen on the owner's Mac, 2026-09-20: "EA v1.08 matches,
    but the source was modified 5951 min after this build was compiled".

    The two kinds of stale are kept apart deliberately. A version mismatch is
    certain (the binary is a different build). Drift is weaker evidence: it
    fires on any saved edit, including ones that never reach a terminal. Both
    are worth showing; conflating them would let the weaker one speak with the
    stronger one's authority.
    """

    def test_drift_is_stored_on_the_bridge(self):
        bridge = _bridge()
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(offset_minutes=-180),
        })

        assert bridge.ea_source_drift_s is not None
        assert bridge.ea_source_drift_s > 3 * 3600 - 60

    def test_a_freshly_compiled_build_records_no_drift(self):
        bridge = _bridge()
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(),
        })

        assert not bridge.ea_source_drift_s

    def test_a_save_within_the_slack_records_no_drift(self):
        """Same threshold the warning uses, so the badge and the log cannot
        disagree about whether this build is stale."""
        bridge = _bridge()
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(offset_minutes=-0.5),
        })

        assert not bridge.ea_source_drift_s

    def test_an_unreadable_stamp_records_no_drift_rather_than_guessing(self):
        bridge = _bridge()
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": "not a timestamp",
        })

        assert not bridge.ea_source_drift_s

    def test_a_version_mismatch_does_not_also_claim_drift(self):
        """It returns before the drift check, and must not leave a stale
        value from a previous connection behind either."""
        bridge = _bridge()
        bridge._check_ea_version({
            "ea_version": ea_bridge._expected_ea_version(),
            "compiled": _stamp(offset_minutes=-180),
        })
        bridge._check_ea_version({"ea_version": "0.01", "compiled": _stamp()})

        assert not bridge.ea_source_drift_s
