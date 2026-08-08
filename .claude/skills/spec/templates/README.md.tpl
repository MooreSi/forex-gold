# {{Feature name}}

**Spec:** {{[docs/specs/NNN-slug.md](../../../specs/NNN-slug.md) | none — see "What we're building" below}}
**Status:** planning (pre-implementation)
**Domain:** {{domain}}
**Touches money:** {{no | YES — tasks {{0N0}}, {{0N0}}. `/safe-change` governs those; owner sign-off + demo session required before they are Done.}}
**Created:** {{YYYY-MM-DD}}

## 👋 Picking this up (agents start here)

1. **Read the rules first** — [CLAUDE.md](../../../../CLAUDE.md) and
   [docs/system/rules/10-golden-rules.md](../../../ai/10-golden-rules.md). This app places real orders with
   real money.
2. **Read the plan** — {{the anchor spec for Problem/Goal/what-must-NOT-change;}} this hub for the
   index + decisions{{; `SUMMARY.md` for the plain-English digest}}{{; `REVIEW.md` for the evidence}}.
3. **Check [PROGRESS.md](PROGRESS.md)** — the shared status log. See what's done / in progress / free.
4. **Claim your task** in PROGRESS.md: set its row to `in progress`, add your name + date under Owner.
5. **Do the work** from the task file (`0N0-*.md`) — tests first, watch them fail, then implement.
6. **Update PROGRESS.md** as you go — `done` (with commit) or `blocked` (say why). This is how
   everyone sees where the work is.

Gates: `/safe-change` before touching anything that can move money · `/add-tunable` when a number
should be user-editable · `/split-file` if the target file is over 800 lines · `python -m tools.checks all`
before every commit.

## What we're building & why

{{2–4 paragraphs: the problem in concrete terms, the shape of the solution, and why this approach
over the obvious alternative. Written for a future session picking this up cold. If there is an
incident behind it, say what happened and when.}}

## What must NOT change

{{The single most important section. Which behaviour stays byte-identical, which defaults must not
move, which tests must pass unmodified. If the anchor spec covers this, point at it and repeat only
the lines that constrain the tasks in this pack.}}

- {{behaviour that stays exactly as it is}}

## Doc index

| Doc | Contents |
|---|---|
| [PROGRESS.md](PROGRESS.md) | Live shared status log |
| {{[SUMMARY.md](SUMMARY.md)}} | {{Plain-English digest of every change (owner-facing) — if present}} |
| {{[QUESTIONS.md](QUESTIONS.md)}} | {{Decisions to confirm / answered — if present}} |
| {{[REVIEW.md](REVIEW.md)}} | {{Evidence + current-state snapshot — if present}} |
| {{[BAR.md](BAR.md)}} | {{Screen bar for the UI surface — if present}} |
| [010-{{slug}}.md](010-{{slug}}.md) | {{one-line summary}} |
| [020-{{slug}}.md](020-{{slug}}.md) | {{one-line summary}} |

{{Keep this table in sync — every numbered file + companion doc in the pack gets a row. For a phased
pack, list the phase dirs here instead and give each phase its own README index.}}

## Roadmap

| # | Task | Depends on | Money | Ships with |
|---|---|---|---|---|
| {{010}} | {{name}} | — | {{no}} | — |
| {{020}} | {{name}} | {{010}} | {{no}} | {{— / 030}} |

## Decisions locked with the user ({{YYYY-MM-DD}})

| Decision | Choice | Source |
|---|---|---|
| {{decision}} | {{what was chosen and, briefly, why}} | {{user / safe-change / evidence in REVIEW.md}} |

## Building blocks we reuse (do not rebuild)

| Need | Existing code |
|---|---|
| {{capability}} | {{`backend/src/services/<area>/<file>.py:line` — what it already does}} |

## Out of scope

- {{explicitly punted item, and where it lives if deferred (a later pack, phaseN/, …)}}

## Open questions

{{Answered items stay here annotated "(answered YYYY-MM-DD: …)" — kept for history, not deleted.
Full decision write-ups live in QUESTIONS.md; this is the short list.}}

- {{open question — with the current default if we had to proceed}}
