"""A settings field the operator can type into must reach the column it names.

Every other test in `tests/api/routers/test_settings.py` monkeypatches the
controller, which is right for what those tests are about — redaction, honest
echoes, refusals — and blind to the one thing that breaks a settings form in
practice: **the router and the store disagreeing about the shape of a write.**
A recorder accepts any signature and any key, so a router that calls
`save_telegram_config({...})` against a real
`save_telegram_config(bot_token, chat_id, enabled)` passes every one of them
and raises `TypeError` the first time an operator presses Save.

So these tests bind against the REAL callables and the REAL schema. Nothing
here opens the application database: the SQL is never executed, only its column
list is read from `backend/migrations/schema_sql.py`, and the signatures come
from `inspect`.

Two failures found when this file was written (2026-09-18), both introduced by
the React port and both invisible until now:

  * `PUT /api/settings/telegram` passed one dict to a three-parameter function;
  * the Connections tab wrote `recipient`, which `email_config` has never had —
    the column is `to_addr`.

`test_the_dashboard_only_offers_fields_the_store_has` is the one that catches
the second class for good. It reads the tab's own field list rather than a copy
of it, so a field added to the UI and not to the schema goes red here rather
than at the operator's keyboard.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

# The STORE's signatures, not the controller's. Every controller operation is
# a `(*args, **kwargs)` forwarder by design, and `bind` against `*args` accepts
# literally anything — so binding there proves nothing at all. It is the same
# trap the controller forwarding sweep hit: the sweep captures each service
# function and binds against that, and so does this file.
from backend.src.services.notifications import repo as _email_repo
from backend.src.services.telegram import repo as _telegram_repo

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _columns_of(table: str) -> set[str]:
    """The column names of a table, read from the schema this app creates.

    Deliberately not `PRAGMA table_info` against a live database: that would
    describe the machine this test happens to run on, including columns a
    half-applied migration left behind. The schema file is the claim.
    """
    sql = (_REPO / "backend" / "migrations" / "schema_sql.py").read_text(encoding="utf-8")
    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S
    )
    assert body, f"no CREATE TABLE for {table}"
    cols = {
        m.group(1)
        for line in body.group(1).splitlines()
        if (m := re.match(r"\s{4}(\w+)\s+(?:TEXT|INTEGER|REAL)", line))
    }
    # Columns added after the first release live in the migration steps.
    steps = (_REPO / "backend" / "migrations" / "steps.py").read_text(encoding="utf-8")
    cols |= set(re.findall(rf"ALTER TABLE {table} ADD COLUMN (\w+)", steps))
    assert cols, f"parsed no columns for {table}"
    return cols


def _tab_source() -> str:
    return (_REPO / "frontend" / "src" / "components" / "settings" / "tabs"
            / "ConnectionsTab.tsx").read_text(encoding="utf-8")


def _fields_written_to(path: str) -> set[str]:
    """The `key:` values of the domain block whose `path:` is `path`.

    The tab renders its form from a literal list of specs, so the list IS the
    set of names the browser can PUT. Reading it here rather than restating it
    is the whole point: a restated copy drifts, and drifting silently is the
    failure this file exists to stop.
    """
    src = _tab_source()
    start = src.index(f'path: "{path}"')
    end = src.find("path: \"", start + 10)
    block = src[start:end if end != -1 else len(src)]
    return set(re.findall(r'\{\s*key:\s*"(\w+)"', block))


# ── The signature the router has to satisfy ──────────────────────────────────

class TestTheRouterCallsTheStoreCorrectly:
    """Bound against the real function, the way the controller sweep binds.

    `inspect.signature(...).bind(...)` raises exactly where a live Save would,
    without touching a database or sending anything.
    """

    def test_a_telegram_write_binds_against_the_real_save(self):
        """The bug: the router passed one dict to `(bot_token, chat_id,
        enabled)`. `bind` raises "missing a required argument: 'chat_id'",
        which is the TypeError the operator would have got on Save.
        """
        sig = inspect.signature(_telegram_repo.save_telegram_config)

        with pytest.raises(TypeError):
            sig.bind({"bot_token": "123:ABC", "chat_id": "-100", "enabled": True})

        sig.bind(bot_token="123:ABC", chat_id="-100", enabled=True)

    def test_the_router_does_not_pass_the_body_as_one_object(self):
        """The shape the handler actually uses, read from its source.

        Asserted structurally because the router's call goes through a
        `(*args, **kwargs)` controller, so no runtime check between the two can
        see the mismatch — which is exactly why it survived the port.
        """
        src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((_REPO / "backend" / "src" / "api" / "routers").glob("*.py"))
        )
        call = re.search(r"save_telegram_config\((.*?)\)\n", src, re.S)

        assert call, "no save_telegram_config call in the API layer"
        assert "model_dump()" not in call.group(1), (
            "the router hands the whole body to a three-parameter save"
        )

    def test_an_email_write_binds_against_the_real_save(self):
        sig = inspect.signature(_email_repo.save_email_config)

        sig.bind({"to_addr": "me@example.com"})


# ── The column the field has to name ─────────────────────────────────────────

class TestTheDashboardOnlyOffersFieldsTheStoreHas:
    def test_every_email_field_is_a_column(self):
        """`recipient` was not. The column is `to_addr`, and an operator who
        typed an address into that box got a 500 and no saved address."""
        unknown = _fields_written_to("/api/settings/email") - _columns_of("email_config")

        assert not unknown, (
            f"Connections tab writes {sorted(unknown)} to email_config, which "
            "has no such column — the write raises and the value is lost"
        )

    def test_every_telegram_bot_field_is_accepted_by_the_save(self):
        """The bot's three settings, named as the save names them.

        `bot_token_enc` is the column; the save takes it as `bot_token`, so the
        parameter list is the contract here, not the schema.
        """
        accepted = set(inspect.signature(_telegram_repo.save_telegram_config).parameters)
        offered = _fields_written_to("/api/settings/telegram")

        assert offered <= accepted, (
            f"Connections tab writes {sorted(offered - accepted)} to the "
            f"telegram bot config, which accepts {sorted(accepted)}"
        )


class TestTheScannerCanSee:
    """Negative controls. Every assertion above is `not unknown` or `<=`, which
    an empty set satisfies — so a parser that found nothing would report the
    whole tab as correct."""

    def test_it_finds_the_email_fields(self):
        assert len(_fields_written_to("/api/settings/email")) >= 3

    def test_it_finds_the_telegram_fields(self):
        assert len(_fields_written_to("/api/settings/telegram")) >= 2

    def test_it_finds_the_email_columns(self):
        cols = _columns_of("email_config")

        assert {"smtp_host", "to_addr", "send_time"} <= cols
        assert "recipient" not in cols

    def test_a_column_added_by_a_migration_is_seen(self):
        """`orb_report_enabled` exists only as an ALTER. A parser that read the
        CREATE alone would report a legitimate field as unknown."""
        assert "orb_report_enabled" in _columns_of("email_config")

    def test_a_table_that_does_not_exist_is_an_error_not_an_empty_set(self):
        with pytest.raises(AssertionError):
            _columns_of("table_this_app_has_never_had")


# ── The same check, for every screen that writes the risk row ────────────────

RISK_SPEC_FILES = {
    "frontend/src/components/settings/content/risk.ts": "the Risk tab",
    "frontend/src/components/engines/content/capabilities.ts":
        "the Signal Generator's capability switches",
    "frontend/src/components/parsing/content/settings.ts": "the parsing switches",
    "frontend/src/components/parsing/internal/SignalsSourcesSection.tsx":
        "the live-execution gates",
}


def _risk_columns() -> set[str]:
    """Every column of `vantage_risk_settings`, including migrated ones."""
    cols = _columns_of("vantage_risk_settings")
    recent = _REPO / "backend" / "migrations" / "steps_recent.py"
    if recent.exists():
        cols |= set(re.findall(
            r"ALTER TABLE vantage_risk_settings ADD COLUMN (\w+)",
            recent.read_text(encoding="utf-8")))
    return cols


def _keys_in(path: str) -> set[str]:
    """Every `key: "..."` in a screen's spec list."""
    src = (_REPO / path).read_text(encoding="utf-8")
    return set(re.findall(r'\bkey:\s*"(\w+)"', src))


@pytest.mark.parametrize("path,what", sorted(RISK_SPEC_FILES.items()))
def test_every_risk_setting_a_screen_offers_is_a_real_column(path, what):
    """`update_risk_settings` runs `UPDATE vantage_risk_settings SET <keys>`.

    A key that is not a column raises, so the operator gets a 500 and the
    setting is silently not saved — while the box on screen still shows what
    they typed until the next reload.

    Found on 2026-09-18 in the Risk tab, which offered `risk_pct` (a column on
    a different table entirely) and `daily_loss_limit_pct` (a column nowhere at
    all). Two of its four fields threw on save. The real names are
    `risk_per_trade_pct` and `max_daily_loss_pct`.
    """
    unknown = _keys_in(path) - _risk_columns()

    assert not unknown, (
        f"{what} offers {sorted(unknown)}, which vantage_risk_settings has no "
        "column for — every save of one of those raises and loses the value"
    )


def test_the_column_reader_sees_the_risk_table(path=None):
    """Negative control. Every assertion above is `not unknown`, which an empty
    column set would satisfy for any screen at all."""
    cols = _risk_columns()

    assert {"risk_per_trade_pct", "max_daily_loss_pct", "max_open_trades"} <= cols
    assert "daily_loss_limit_pct" not in cols
    assert len(cols) > 50, "the risk row is wide; a short list means a bad parse"


@pytest.mark.parametrize("path,what", sorted(RISK_SPEC_FILES.items()))
def test_each_screen_actually_declares_some_keys(path, what):
    """The other half of the negative control: a spec file whose shape changed
    would silently offer nothing to check."""
    assert _keys_in(path), f"no `key:` entries parsed out of {what}"
