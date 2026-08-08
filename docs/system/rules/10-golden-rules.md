# The golden rules

**Read this before changing anything. It applies to every AI agent and every
human, on every change, without exception.**

This app places real money orders on a live broker account. Most rules below
exist because something already went wrong once. Where that is true, the
incident is named — those are not hypotheticals.

---

## The five that can cost money

### 1. Never place, close or modify a real or demo MT5 order to test something

Not "carefully", not "just once", not "on demo". Tests use fakes and
sentinels. If you cannot test a change without touching a broker, that
change needs a human at a demo terminal — stop and say so.

### 2. The close path does not get "improved"

`close_trade`, `record_close`, `_make_close_trade_ctx`, `partial_close_trade`
and `_schedule_profit_sync` may be renamed or relocated verbatim. They may
**not** be reshaped — no argument added, removed, reordered or defaulted, no
branch restructured — without explicit sign-off from the owner plus a demo
session watching real trades open and close.

`tests/core/test_close_trade_characterization.py` is the witness. It must
pass **unmodified**. If your change requires editing it, your change is not
allowed.

### 3. Defaults never change silently

Any behaviour constant that becomes configurable keeps its exact previous
value as the default. An upgrade must never change how the system trades.
See `docs/system/rules/60-adding-a-tunable.md`.

### 4. A failing test is information, not an obstacle

Never delete, skip, `xfail`, loosen or rewrite a test to make a change pass.
If a test fails, either your change is wrong or the test encodes a rule you
did not know about. Both mean stop and read it.

The only legitimate reason to edit a characterization test is a **mock-target
relocation** — the function moved, so the patch target moves with it — and
the commit must say so explicitly.

### 5. Money maths is not a place to be clever

Balances, P&L and prices are floats today (a known issue — see
`docs/decisions/`). Do not "tidy" rounding, change an epsilon, or reorder
arithmetic in a sizing or P&L path. Those changes are invisible in review and
show up as missing cents that compound.

---

## The five that keep the codebase alive

### 6. Test-first, and watch the test fail

Write the test. **Run it. See it fail for the reason you expect.** Then
implement. A test that has never failed has never proved anything — it may be
asserting nothing at all.

This is not a style preference here. The previous refactor shipped guardrails
that enforced nothing (`delegation_checker.py` scanned a deleted directory and
printed "all good" on every run for months) precisely because nobody watched
them fail.

### 7. Every claim gets a negative control

If you assert "there are zero violations", also assert your scanner can find
one. A green check from a broken checker is worse than no check, because it
buys false confidence.

### 8. Layers only point downward

```
frontend/  →  controllers/  →  services/  →  db/  →  (nothing)
                                utils/, config/  →  (nothing)
```

The frontend never imports `backend.src.db`. Controllers never import a
service's `repo`. `utils/` and `config/` import nothing above them. These are
enforced — see `docs/system/rules/30-architecture.md`.

### 9. Delete code, don't comment it out

Dead code that looks live is how this codebase accumulated 3,000 lines of
unreachable methods. Git remembers. If it is not called, remove it.

### 10. Say what you actually did

If you skipped part of the task, say which part and why. If a number is worse
than the target, report the number, not the intent. If you are unsure a change
is safe, say that instead of shipping it with confident wording.

A report that overstates completion is the single most expensive thing an
agent can produce here — the last one cost a full re-audit.

---

## Hard "do not" list

| Never | Because |
|---|---|
| `git push --force` to a shared branch | rewrites history others hold |
| Commit secrets, tokens, licence keys, `.env` | they end up in the remote permanently |
| Add a licence bypass or auth "test mode" | that is the security control, not an obstacle |
| Weaken a ratchet baseline to make CI pass | the baseline is the record; raise it only with a stated reason |
| Run two full test suites at once | produces phantom failures — known, reproducible |
| `pip install` a new runtime dependency without asking | this app ships to a Windows installer |
| Edit anything under `docs/history/` | it is an audit trail of what was true at the time |
| Rewrite historical docstrings ("extracted from core/engine.py's X") | they describe a file that really had that name; changing them falsifies the trail |

---

## When to stop and ask

Stop and ask the owner when:

- the change touches order placement, closing, or position sizing
- a test would have to be modified to pass
- a ratchet baseline would have to rise
- the work needs a real or demo broker connection to verify
- you are about to say "this should be fine" about money

Asking costs a message. The alternatives have cost real money in this
codebase before.
