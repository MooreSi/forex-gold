"""Column names interpolated into SQL have to be identifiers, not text.

Nine repos build an UPDATE's SET clause from the keys of a dict a caller hands
them:

    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE vantage_risk_settings SET {set_clause} WHERE id=1", ...)

The *values* are parameterised and safe. The **column names are not** — they go
into the statement verbatim.

Nothing exploits this today. The one path that takes a dict from outside the
app — a settings proposal over the sync channel — filters it through
`_SYNCED_SETTINGS_KEYS` first, and that filter has its own tests. But the
filter lives in a different module from the SQL, so the safety of the query
depends on every caller, present and future, remembering. `dpm/repo.py` already
shows the alternative: it checks `column not in MILESTONE_COLUMNS` and returns.

This is that check, in one place, for the callers that cannot enumerate their
columns. It is deliberately about SHAPE, not a column allowlist: a name that is
a valid identifier but not a real column still fails, loudly, with SQLite's own
"no such column" — which is a clear error rather than a corrupted statement.
"""
from __future__ import annotations

import pytest

from backend.src.utils.sql_identifiers import set_clause_for


class TestItBuildsTheClause:
    def test_a_single_column(self):
        assert set_clause_for(["max_open_trades"]) == "max_open_trades=?"

    def test_several_columns_keep_their_order(self):
        assert set_clause_for(["a", "b_2", "c"]) == "a=?, b_2=?, c=?"

    def test_a_dict_may_be_passed_directly(self):
        assert set_clause_for({"risk_per_trade_pct": 0.5}) == "risk_per_trade_pct=?"

    def test_leading_underscores_are_fine(self):
        assert set_clause_for(["_internal"]) == "_internal=?"


class TestItRefusesAnythingThatIsNotAnIdentifier:
    """Each of these would otherwise become part of the statement."""

    @pytest.mark.parametrize("bad", [
        "max_open_trades=99, risk_per_trade_pct",   # a second assignment
        "x WHERE 1=1",                              # widening the target
        "x, (SELECT 1)",                            # a subquery
        'x"',                                       # closing an identifier quote
        "x'",
        "x;",                                       # statement chaining
        "x--",                                      # trailing comment
        "x /* c */",
        "x y",                                      # a space at all
        "",                                         # empty
        "1abc",                                     # not an identifier
        "table.col",                                # qualified names not accepted
    ])
    def test_it_raises(self, bad):
        with pytest.raises(ValueError) as excinfo:
            set_clause_for([bad])

        assert "identifier" in str(excinfo.value).lower()

    def test_a_non_string_key_is_refused(self):
        with pytest.raises(ValueError):
            set_clause_for([1])

    def test_the_message_names_the_offender(self):
        """So the failure says which key, not just that one was wrong."""
        with pytest.raises(ValueError) as excinfo:
            set_clause_for(["good_one", "bad one"])

        assert "bad one" in str(excinfo.value)

    def test_one_bad_key_refuses_the_whole_clause(self):
        """Not a filter. Dropping the bad key silently would apply a partial
        update the caller believes was complete."""
        with pytest.raises(ValueError):
            set_clause_for(["max_open_trades", "x; DROP TABLE t"])


class TestAnEmptyUpdateIsTheCallersProblem:
    def test_no_columns_raises_rather_than_producing_broken_sql(self):
        """`UPDATE t SET  WHERE id=1` is a syntax error at execute time, far
        from the code that caused it."""
        with pytest.raises(ValueError):
            set_clause_for([])
