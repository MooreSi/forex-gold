"""On a template-managed channel, nothing may say "awaiting follow-up".

bugs/023 removed that promise from the Telegram alert, because
`instant_followup.py`'s `managed_by == "ea"` skip means no follow-up SL/TP is
EVER applied to a template-managed trade -- the promise structurally could not
be kept. Two other places kept making it:

* the note stored on the signal row, read back in the Pending Signals panel and
  in any later investigation;
* the `[IME] Instant ... (provisional)` log line.

**Found running demo 12 on 2026-09-09**, where it did exactly the damage the
wording implies: the log said "(provisional)" on a trade whose stop had come
from the template, and the first reading of it was that the demo had failed.
The stored note said `5.0 pts` -- which IS `sl_pips=50` on the governing
template -- while describing itself as provisional and awaiting a follow-up
that cannot arrive.

The runbook's own fail condition for demo 12 is "the words 'awaiting follow-up'
on a template-managed channel". It was true in two places while the demo
passed.

**What these tests do NOT prove.** They read source text, so they cannot see
reachability: making the template branch `if False:` kills only ONE of the four
(`test_the_promise_is_conditional_now`), because the other three still find the
strings they look for sitting right there. That was measured, not assumed. A
behavioural test would need `execute_instant_entry` driven end to end with a
template bound, which is worth doing if this wording ever matters more than it
does today -- it is a note and a log line, and no money depends on it.
"""
from __future__ import annotations

import inspect

from backend.src.services.trading import instant_entry


def _source() -> str:
    """Source with comment lines stripped.

    The first version of this file grepped the raw source, and the explanatory
    COMMENT added by the fix contained the very string the test forbade, so a
    correct fix failed its own test. Same trap as
    `structural-tests-match-comments`: a source-text assertion must read code,
    not the prose next to it.
    """
    out = []
    for line in inspect.getsource(instant_entry).splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


class TestTheStoredNoteIsTemplateAware:
    def test_the_note_is_not_one_unconditional_string(self):
        """If the note is built inline in the insert call, it cannot vary by
        template -- which is exactly how this survived the bugs/023 fix."""
        src = _source()

        assert "_ime_note" in src, (
            "the signal note is still built inline and cannot depend on "
            "whether a template governs the trade"
        )

    def test_a_template_note_names_the_template(self):
        """The note must carry the template's NAME, not the word "template".

        The looser version of this passed with the whole branch made
        unreachable, because the word appears in the non-template text too.
        """
        src = _source()
        start = src.index("_ime_note")
        block = src[start:start + 900]

        assert "_tpl_name_ime" in block, (
            "the stored note never interpolates the template's name"
        )

    def test_the_promise_is_conditional_now(self):
        """'awaiting follow-up' may still appear -- for non-template trades,
        where a follow-up genuinely can arrive."""
        src = _source()
        idx = src.index("awaiting follow-up SL/TP")
        before = src[max(0, idx - 700):idx]

        assert "_template_ime" in before, (
            "'awaiting follow-up SL/TP' is not guarded by a template check"
        )


class TestTheLogLineToo:
    def test_the_log_line_does_not_hardcode_provisional(self):
        """It said "(provisional)" on a stop taken from the template."""
        src = _source()
        line = [l for l in src.splitlines() if "[IME] Instant %s executed" in l]

        assert line, "the IME execution log line has moved or been renamed"
        assert "provisional" not in line[0], (
            f"the IME log line still calls every stop provisional: {line[0].strip()}"
        )
