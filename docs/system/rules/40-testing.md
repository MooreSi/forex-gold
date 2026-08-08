# Testing — the protocol

This is the contract. `/test` is the working skill that applies it — the rules
in full, the directory layout, the anti-pattern table and the review checklist.
Use `/test` when writing or reviewing; read this when you want to know why.

## Red, green, then commit

1. **Write the test first.**
2. **Run it. Watch it fail.** Confirm the failure message is the one you
   expected — not an import error, not a typo.
3. Implement the smallest change that passes.
4. Run the test. Watch it pass.
5. Run the full suite before committing.

Step 2 is the one people skip, and it is the one that matters. A test that has
never been red has never proved it can detect anything. This repo has already
shipped a "guardrail" that scanned a deleted directory and reported success on
every run for months — nobody had watched it fail.

## Every green assertion needs a negative control

If you assert a set is empty, also assert your detector can find a member.

```python
def test_no_production_code_reaches_into_a_runtime_private():
    assert _find_private_reaches() == []

def test_the_leak_scan_can_actually_see_a_leak():
    """Negative control: a zero-offender assertion is worthless if the
    scanner is blind."""
    assert pattern.search("await engine._some_private(a, b)")
    assert not pattern.search("await self._bridge._get_positions()")
```

Both, always. The first is the rule; the second is what makes the first mean
something.

## Test types used here

| Type | Purpose | Naming |
|---|---|---|
| **Characterization** | Pins existing behaviour *before* it moves. Written against unmodified code. | `test_X_characterization.py` |
| **Surface** | Tests the extracted service function directly. | `test_X_surface.py` |
| **Wiring / relocation** | Proves the caller reaches the right thing with the right bindings. | `test_X_relocation.py` |
| **Structural** | Pins the shape — what may not grow back. | `tests/refactor/` |

When a body moves, the characterization test stays as-is and a surface test
covers the new home. Delete a characterization test **only** when an
identically-named surface twin exists and you have run it green first — and
say so in the commit.

## Where tests live

The tree mirrors `backend/src/`. A test's directory is decided by the directory
of the module it covers — `services/trading/close_trade.py` is tested by
`tests/services/trading/test_close_trade.py`. Every directory needs
`__init__.py`; basenames repeat across service dirs and without it pytest
imports two files as one module and silently runs only one.

**`tests/core/` is legacy and closed.** `backend/src/core/` no longer exists —
it was dissolved into `services/` and the tests did not follow. Never add a file
there. When you touch one, move it to the directory mirroring its subject as
part of that change. `python -m tools.test_layout.migrate` prints the mapping.

## Never do this to a test

- delete it because it fails
- add `@pytest.mark.skip` / `xfail` to get past it
- loosen an assertion (`==` → `>=`, exact string → `in`)
- widen a tolerance
- comment out the assert

If a test fails, either the change is wrong or the test knows something you
don't. Read it.

The **one** legitimate edit to a characterization test is a **mock-target
relocation**: the function moved, so `mock.patch("old.path.fn")` becomes
`mock.patch("new.path.fn")`. Same function, same signature, new home. Name it
in the commit message.

## Fixtures must match reality

Several fixtures here build a partly-initialised object via
`TradingRuntime.__new__(TradingRuntime)` and set only the attributes the test
happens to need. That is fragile: it passes until a code path reads one more
attribute.

If a fixture needs a new attribute because production code binds it, set what
`__init__` actually sets. That is a fixture becoming *more* faithful, not a
test being bent — and it changes no assertion. Say so in the commit.

## Safety in tests

**No test may place, close or modify a real or demo MT5 order.**

- Bridges are fakes with canned responses.
- Order-path collaborators are sentinels that record calls.
- If a test file could reach a real broker call, it says so in its module
  docstring and proves it cannot.

Example, from `test_bridge_process_relocation.py`:

```python
def test_no_test_in_this_file_can_spawn_a_process():
    """Guard rail, asserted rather than assumed."""
    import subprocess
    assert bp.subprocess.Popen is subprocess.Popen
```

## Determinism

- The market clock is pinned by a pytest plugin (`tools/testing/fixed_clock.py`),
  so `detect_session` and weekend checks are stable.
- Never build a "now" timestamp at module import — by the time a slow suite
  reaches your test it is stale. Compute it inside the test.
- Caches register invalidators so a demo/live switch does not serve stale
  values.

## Running

```bash
pytest tests/ -q                 # full suite, ~4-5 minutes
pytest tests/core/ -q            # the trading logic
pytest tests/refactor/ -q        # the structural gates
pytest tests/frontend/ -q        # pages import, app boots and serves
```

**Never run two full suites at once.** It produces phantom failures — known
and reproducible here.

## Coverage

```bash
pytest tests/ -q --cov=backend --cov=frontend --cov-report=term-missing
```

Coverage is a ratchet, not a target: see `tools/refactor_audit/coverage_gate.py`.
It may go up; it may not go down. A number that only ever rises is worth more
than a threshold nobody can hit.

Coverage measures *lines executed*, not *behaviour verified*. A file at 90%
with no assertions is untested. Use it to find the zero-coverage holes, not to
declare victory.
