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

For anything beyond a one-line fix, write or find a spec **in `docs/todo/`** —
a plan pack's `SPEC.md` (`/spec`) or a standalone spec file (`/new-spec`).
A spec is half a page: what changes, what must
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

**`python` here means `.venv/bin/python`.** Bare `python` is a different
interpreter with a different package set, and it fails in a way that reads
as a finding about your change — see CLAUDE.md, Session mechanics.

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

## 7. Releasing a version — About > Version history goes with it

Whenever the version changes, **the same commit** carries all four, or none:

1. a new **first** entry in `backend/src/utils/version_history.py` `RELEASES`
   — `(tag, title, colour, badge, [changes])`, user-facing bullets of more
   than 40 characters each. This is what Settings, the header, the admin
   console AND **About > Version history** read;
2. the matching `## vX.YYY — <title> (date)` section at the top of
   `CHANGELOG.md` (the GitHub release body, via `tools/publish_release.py`);
3. the pin test moved: `tests/core/test_version_is_<x>.py` renamed and its
   strings updated, previous releases still asserted;
4. a look at the About page: `GET /api/system/releases` must answer with
   `releases[0].version == version` and named fields
   (`tests/api/routers/test_system.py` checks both).

The GitHub release is created by `.github/workflows/release.yml` on the push;
check it landed with `gh release view vX.YYY -R MooreSi/forex-gold`.

Why: until 2026-10-21 the About page showed "—" for every release with no
notes, because the route sent `RELEASES`' raw tuples and nobody looked at the
page after a bump. The owner reported it as "isn't updating".

### Every install route must leave a machine able to update itself

Updates are `git fetch` + `checkout`, so a machine without git can never
update, and an admin-console push to it fails with "git not found on PATH".
The app does not assume an installer gave it git: on Windows,
`core_git_install.ensure_git()` downloads PortableGit at startup when none is
found (spec 027). That exists because a third-party repackaged zip ran neither
of our installers (2026-10-23).

So, on any release:

- a new way to install or package the app (a zip, a script, an image) must
  end with git on the machine and the files a checkout, or rely on the app's
  own `ensure_git()` and say so;
- the PortableGit URL **and SHA-256** are pinned in three places that move
  together: `backend/src/services/positions/core_git_install.py`,
  `Setup & Start FOREX.bat` and `installer/FOREX_Trader_Setup.iss` (URL only;
  the .iss does not check the hash yet). Take the hash from the release's own
  asset digest. `tests/positions/test_git_self_install.py::TestOnePinEverywhere`
  fails when they disagree.

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
