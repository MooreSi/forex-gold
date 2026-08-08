# How to make a change, end to end

The short version:

> spec → failing test → smallest change → full suite → gates → commit

If a step is skipped, say which one and why, in the commit.

---

## 0. Understand before you type

Read the file you are about to change **and** its tests. This codebase's
comments carry incident history — "fixed 2026-07-24, a template-assigned
channel's signals were silently executing as Limit Runner" — and that context
is usually the reason the code looks the way it does.

If a comment explains why something is odd, it is odd on purpose.

## 1. Is there a spec?

For anything beyond a one-line fix, write or find a spec in `docs/specs/`.
See `docs/specs/TEMPLATE.md`. A spec is half a page: what changes, what must
not change, how you will know it worked.

Specs exist so the *intent* survives the conversation. A chat transcript is
not a record.

## 2. Classify the change

| Kind | What it needs |
|---|---|
| Bug fix | a test reproducing the bug first |
| New behaviour | a spec + tests |
| Refactor | characterization tests before moving anything |
| Config/tunable | see `docs/system/rules/60-adding-a-tunable.md` |
| Anything touching orders | **stop** — owner sign-off + demo session |

## 3. Write the failing test

See `docs/system/rules/40-testing.md`. Watch it fail for the right reason.

## 4. Make the smallest change that passes

Resist the tidy-up. A refactor bundled with a fix means neither can be
reviewed, and if it breaks you cannot tell which half did it.

## 5. Verify — all of it

```bash
pytest tests/ -q
python -m tools.refactor_audit.structure_gates   --check
python -m tools.refactor_audit.import_contracts  --check
python -m tools.refactor_audit.facade_audit      --check
python -m tools.refactor_audit.orphan_detector
python -c "import backend.src.app"
```

Or, in one go:

```bash
python -m tools.checks all
```

**A gate that fails is not noise.** If a baseline genuinely has to rise, say
why in the commit and only then `--update-baseline`. Raising a ratchet
quietly defeats its entire purpose.

## 6. Commit

Explain **why**, not what — the diff already shows what. If the change was
subtle, say what would have gone wrong without it.

```
services/risk: clamp the R:R floor to its declared range

A value of 0 would open trades the pre-trade filter currently refuses,
and the UI is not the only writer -- node sync applies values too. The
clamp belongs in the service so every path goes through it.

Verified: suite 1989 passed - gates green.
```

Never mention which AI model produced the change.

---

## Things that look safe and are not

**"I'll just widen this timeout."** Several timeouts here are the difference
between a trade being managed and being abandoned. Check whether it is in
`EXPERT_PARAMS` first — if it is, it is a config change, not a code change.

**"This constant should be configurable."** Probably true, and there is a
process: `docs/system/rules/60-adding-a-tunable.md`. Do not hardcode a second copy.

**"The test is flaky, I'll rerun it."** Rerun once. If it fails again, it is
not flaky. Known real flakiness sources here: module-level timestamps, and
running two suites concurrently.

**"This file is huge, let me split it while I'm here."** File splits are their
own change with their own risks — see `docs/system/rules/70-file-organisation.md`. Do
not bundle one with a behaviour change.

**"Coverage dropped 0.1%, that's fine."** It is a ratchet. Add the test.
