# {{Feature name}} — PROGRESS

**Shared status log. Any agent picking up a task updates this file** — claim a row (name + date under
Owner), flip its Status as you go, leave a one-line Note (commit / blocker / decision). This is how
every agent sees where the work is. Keep it honest: a task reported Done that isn't is the exact
failure mode this repo's rules exist to prevent.

_Last updated: {{YYYY-MM-DD}} — {{one-line state, e.g. "pack scaffolded, no code started"}}._

## Status key
`not started` · `in progress` · `blocked` (say why) · `done` ({{date + commit}})

A money-touching task is **not** `done` on a green suite alone — it needs owner sign-off and a demo
session, both recorded in Notes.

## Overall
- {{Phase / area}}: not started
- **Gates:** `/safe-change` run on money tasks? {{no | n/a}} · `python -m tools.checks all` green? no
- **Demo session** (money tasks only): {{not done | n/a}}

## Tasks

| {{Phase}} | Task | Money | Status | Owner | Notes |
|---|---|---|---|---|---|
| {{1}} | [{{010 slug}}]({{path}}) | {{no}} | not started | — | {{key decision / value}} |
| {{1}} | [{{020 slug}}]({{path}}) | {{no}} | not started | — | |

## Decisions log
- {{decision → choice (source: user / safe-change / REVIEW.md evidence, YYYY-MM-DD)}}

## Verification log
{{Paste the real `python -m tools.checks all` output (or its tail) each time a task lands. Green
output claimed without the paste is not evidence.}}

- {{YYYY-MM-DD, task 0N0: <result>}}

## Blockers / open
- {{blocker, or "none"}}
