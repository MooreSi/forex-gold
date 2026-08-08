# {{0N0}} — {{Task name}}

**Status:** not started {{| in-progress | Done (YYYY-MM-DD, commit abc1234) | deferred}}
**Depends on:** {{010-{{slug}}.md | none}}
**Touches money:** {{no | YES — run `/safe-change` first. Not Done without owner sign-off + a demo session.}}
**Layer:** {{frontend | controller | service | repo/db | tools/tests}}
**Leverage:** {{existing code/pattern this rides on, or "none — new ground"}}

## Problem

{{What's wrong or missing today, concretely. Cite code with `file.py:line` where it helps.}}

## Decision

{{The chosen approach in 1–3 sentences, and the alternative it beat if that's not obvious.}}

## What must NOT change

{{The behaviour this task must leave byte-identical, and the existing tests that must pass
unmodified. "Nothing — this is new code" is a legal answer; say it explicitly.}}

## Tests first (TDD)

{{The failing tests to write BEFORE implementation — file path + what each asserts. Per
docs/system/rules/40-testing.md: run them, watch them fail, confirm the failure is the one you expected.
Every green assertion needs a negative control — if you assert a set is empty, also assert the
detector can find a member. No test may place, close or modify a real or demo MT5 order.
Pure-docs tasks: "N/A — docs only".}}

- {{`tests/<area>/test_<name>.py::test_<behaviour>`}} — {{behaviour it pins}} — {{type: characterization / surface / wiring / structural}}
- {{`tests/<area>/test_<name>.py::test_<detector>_can_actually_fail`}} — negative control

## What to do

1. Write the tests above; run them; confirm they fail for the right reason.
2. {{ordered implementation step}}
3. {{…}}
4. `python -m tools.checks all` — suite, four gates, coverage ratchet, boot smoke.

## Where

- {{`backend/src/services/<area>/<file>.py`}} — {{what changes there}}

## Acceptance

- {{observable criterion}}
- **The killer test:** {{the one scenario that proves this works end-to-end, if there is one}}
- `python -m tools.checks all` green, with the real output pasted into PROGRESS.md.

## Notes

{{Edge cases, migration concerns, LOC-gate risk, config/tunable flags, anything a future session
needs. If a number here is a starting value rather than a decided one, say so.}}
