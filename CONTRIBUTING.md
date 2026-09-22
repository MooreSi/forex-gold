# Contributing

Thank you for looking. This is a real trading application: a change merged
here can place an order on somebody's live account, with their money. That is
the whole reason the rules below exist, and why some of them are stricter than
you will be used to.

Everything here is the short version of
[docs/system/rules/](docs/system/rules/). If the two ever disagree, the rules
directory wins.

## Before you write any code

1. Read [the golden rules](docs/system/rules/10-golden-rules.md). It is short
   and it is not optional. Most of it exists because something already went
   wrong once, and the incident is named.
2. Read the domain file for the part you are changing —
   `docs/system/domains/<domain>/README.md`. It holds the constraints, the
   gotchas and the questions that are already settled.
3. For anything bigger than a one-line fix, write the spec first
   (`docs/todo/`). See [50-workflow.md](docs/system/rules/50-workflow.md).

## The rules that will get a pull request rejected

**Never point a test at a real broker.** The suite touches no broker at all —
it uses fakes and sentinels, because it runs unattended on CI and on other
people's machines. There is no exception to this for "just checking".

**Never edit a test to make your change pass.** A failing test means either
your change is wrong or the test encodes a rule you did not know about. Both
mean stop and read it. The one legitimate edit is a mock-target relocation —
the function moved, so the patch target moves with it — and the commit must
say so.

**Write the test first and watch it fail.** A test that has never been red has
never proved anything. If you cannot show it failing for the reason you
expect, it is not testing what you think.

**Do not reshape the close path.** `close_trade`, `record_close`,
`_make_close_trade_ctx`, `partial_close_trade` and `_schedule_profit_sync` may
be moved or renamed verbatim, never restructured, without the owner's sign-off
and a session on a demo account.

**Defaults never change silently.** A constant that becomes configurable keeps
its exact previous value. An upgrade must never change how somebody's system
trades.

**Do not lower a ratchet baseline to get CI green.** The coverage and
structure ratchets only tighten.

## Anything that can move money needs a conversation first

Open an issue before writing code if your change touches order placement,
closing, position sizing, the MT5 bridge or the risk governor. Those need the
owner's sign-off and a demo session, so a pull request that arrives without
one cannot be merged however good it is.
[20-trading-safety.md](docs/system/rules/20-trading-safety.md) is the
protocol.

The same applies to trading policy, risk numbers and anything about how the
money behaves: those are the owner's decisions, and open ones live in
`docs/simon-handover/`.

## Running it locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # dashboard on http://localhost:8888

cd frontend && npm install         # once, per checkout
cd frontend && npm test            # the dashboard's own suite (vitest)
```

Use `.venv/bin/python`, never a bare `python`. A different interpreter with a
different package set produces failures that look real and are not — that
mistake once cost a whole session and 20 fabricated bug reports.

A change under `frontend/src` must rebuild `frontend/dist` in the same commit
(`npm run build`). `dist/` is committed and is what the app actually serves.

## Before you open a pull request

```bash
.venv/bin/python -m tools.checks all
```

That is the full suite, the four structural gates, the coverage ratchet and
the boot smoke test. All of it must pass. A failing gate is not noise.

In the pull request, say what you actually did — including what you skipped
and any number that came out worse than you intended. Green output is not
evidence; that is the lesson this codebase was rebuilt around.

## Licence

By contributing you agree your work is licensed under the
[GNU AGPL-3.0](LICENSE), the same licence as the rest of the project.
