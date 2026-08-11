# Spec pack structure — shapes, companion docs, naming

Reference for `/spec`. Load when scaffolding or reshaping a pack.

## The standard pack shape

```
docs/todo/<domain>/<feature>/          # flat pack (the default)
├── README.md          # the ONLY hub file — onboarding, doc index, decisions, roadmap
├── SPEC.md            # the anchor spec (structure below) — the default for every pack
├── PROGRESS.md        # live status log (see Companion docs) — add when >1 agent will touch the pack
├── 010-<slug>.md      # task files, stepped by 10
├── 020-<slug>.md
└── 030-<slug>.md

docs/todo/<domain>/<feature>/          # phased pack (work spans releases / phases)
├── README.md          # single hub at the feature root
├── SPEC.md            # one anchor spec for the whole pack, all phases
├── PROGRESS.md        # one status log for the whole pack, all phases
├── phase1-<slug>/     # ALL phases get a dir — phase 1 files never sit flat. Descriptive suffix OK.
│   ├── README.md      # phase index + gating condition
│   ├── 010-<slug>.md  # numbering restarts inside each phase dir
│   └── 020-<slug>.md
└── phase2-<slug>/
    ├── README.md
    └── 010-<slug>.md
```

- **Domain dirs are permanent. Feature dirs are temporary** — deleted by `/spec done` once the work
  ships. Git history is the archive.
- **Domain dirs mirror the codebase**, so a pack is findable from the code it changes:
  `trading/`, `risk/`, `broker/`, `signals/`, `dpm/`, `telegram/`, `analytics/`, `backtest/`,
  `frontend/`, `settings/`, `sync/`, `licence/`, `infra/`. Add a new one only when nothing fits.
- **Numbering is always 010-stepped.** Insertions take intermediate numbers (`015-`). No `001-`
  sequences, no `100-` band packs.
- **Feature dir names** are short kebab slugs (`react-rewrite`, `kelly-sizing`); a dated suffix
  (`-june-14`) only for genuinely time-boxed batches.
- **Phase dir names** may carry a descriptive suffix (`phase1-api-layer`, `phase2-shell`) — far more
  navigable than bare `phase1/`. Keep the `phaseN` prefix so order is obvious.

## The anchor spec — `SPEC.md` in the pack

Every pack anchors on a **`SPEC.md` in the feature dir**, structured as in the SPEC.md row below.
It travels with the pack: written at scaffold time, deleted with the pack at `/spec done` (git
history is the archive; anything permanent — the filled Verification checklist, design notes — is
harvested to `CHANGELOG.md` / `docs/system/` first). A standalone `docs/todo/NNN-*.md` is only for
**standalone** `/new-spec` changes that have no pack.

| Lives in `SPEC.md` | Lives in the rest of the pack |
|---|---|
| Problem, Goal, Non-goals | The task breakdown and its ordering |
| **What must NOT change** | Live status, per-task ownership |
| Test plan (the contract) | Open decisions awaiting an answer |
| Rollout, Verification checklist | Evidence snapshots |

The pack README links to `SPEC.md` in its header; the spec's Status moves
Draft → Approved → Building → Shipped as the pack progresses. At `/spec done` the Verification
checklist gets filled in (and harvested) before the pack is deleted.

## Companion docs (the only files allowed beside README + numbered tasks)

`README.md` is the hub. Beyond it and the `0N0-*.md` task files, **only this fixed set** of companion
docs is allowed — each with a defined role. Do **not** invent others (`START.md`, `INFO.md`,
`OVERVIEW.md`, `PLAN.md` are banned — the README is the plan).

| File | Role | When |
|---|---|---|
| `SPEC.md` | **The anchor spec** — Problem / Goal / Non-goals / What must NOT change / Design / Test plan / Rollout / Verification. | Every pack, by default. Skipping it is allowed but must be said explicitly in the README header. |
| `PROGRESS.md` | **Live multi-agent status log** — one row per task (status / owner / notes) + a decisions log + blockers. Every agent that picks up a task claims its row and updates it. | Whenever more than one agent (or session) will work the pack. Default yes for anything non-trivial. |
| `QUESTIONS.md` | **Decisions to confirm** — recommendation-first, plain-language, **inline-answerable** (the user writes `ANSWER:` under each). | When the plan has open decisions the user must settle. Retire/annotate once answered. |
| `SUMMARY.md` | **Owner-facing plain-English digest** — every change, per mechanism, before→after, no jargon or code. For a non-technical reviewer to read the whole pack at a glance. | When the requester isn't the implementer, or the change is broad / behaviour-heavy. |
| `REVIEW.md` | **Evidence + current-state snapshot** — read-only queries against the trade DB, numbers pulled from `latest_logs/`, real trade counts and current values, shareable cold. | For any pack that changes trading behaviour, sizing, thresholds or anything where "is this actually a problem?" needs a number. |

Templates for each live in `templates/`. Keep the README's doc index in sync with whatever exists.

For a pack that builds UI, the user-facing strings (labels, copy) go in `QUESTIONS.md` for the user
to confirm — do not invent and silently settle them.

## Gathering evidence for `REVIEW.md`

**Read-only. `SELECT` only. Never write to the trade DB from a spec session, and never place, close
or modify an order to "see what happens".**

Useful sources:

- The SQLite trade DB (path from `backend.src.config` → `db_path`) — closed trades, per-strategy
  outcomes, realised R, how often a code path actually fired.
- `latest_logs/` and the rotating `forex_trader.log*` in the user data dir — real frequencies,
  real error rates, real timings.
- `config.yaml` / app config rows — the values in force today, which are the "before" column of
  every table in `SUMMARY.md`.

Quote the query and the date you ran it. A number with no query behind it is a guess wearing a
number's clothes.

## The docs phase (user-facing packs)

If the work changes **anything the user reads or relies on**, add a **docs phase** (or a docs task) as
the LAST phase — it documents what the earlier phases actually shipped:

- `CHANGELOG.md` and the in-app Version History (`backend/src/utils/version_history.py`).
- The in-app help surfaces — About, Setup Instructions, Glossary — which are the app's real user
  manual, not an afterthought.
- `docs/system/rules/` when a rule, layer boundary or protocol changed.
- The anchor spec's Verification checklist.

Phased pack → a final `phaseN-docs/` dir. Flat pack → a trailing `0N0-docs.md` task. It is gated on
the implementing phases landing, so it describes shipped behaviour rather than intent.

Decide this in the setup interview: "does this change anything the user reads about?" If yes, plan
the docs phase up front so it isn't forgotten.

## Money-touching tasks

Any task touching order placement, closing, partial closes, position sizing, the risk governor, or
the MT5/EA bridge:

- says **`Touches money: yes`** in its header, and is listed as such in the README header and
  `PROGRESS.md`;
- routes through `/safe-change` before any edit;
- cannot be marked `Done` on a green test suite alone — it needs owner sign-off and a demo session,
  recorded in `PROGRESS.md`.

The close path (`close_trade`, `record_close`, `_make_close_trade_ctx`, `partial_close_trade`) is
frozen: it may be moved verbatim, never reshaped, and only with sign-off. A pack that proposes
reshaping it must say so on the README's first screen, not in a task file three levels down.

## Ordering & coupling notes

- The roadmap table's **Depends on** column captures hard ordering. Add a **"ships with"** note when
  two tasks in different phases must land in the same release (e.g. a UI line that explains a backend
  change).
- Phases are otherwise assumed independent/parallelisable unless a dependency says otherwise.
- A task that would push its target file past the 800-line LOC gate depends on a `/split-file` task
  landing first. Say so rather than discovering it at commit time.
