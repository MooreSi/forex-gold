# The React decision

A record, not a rule. Two dated decisions about the same question, the second reversing the
first. Both are here because a decision without its reasoning gets re-argued on worse
information than the people who made it had.

## 2026-08-06 — rejected

A rewrite of the frontend in React/Next.js with shadcn was proposed and **rejected** by the
owner, on cost:

- a Node runtime added to a Windows installer that bootstraps only Python;
- a second process to supervise on the VPS;
- 17,842 lines rewritten against an HTTP API that did not exist;
- for benefits — SSR, CDN delivery, code-splitting — that do not apply to a single-user
  localhost dashboard.

The two things the proposal was reaching for that **were** worth having — one narrow enforced
boundary to the backend, and slim views composing domain-scoped components — were delivered in
NiceGUI instead, through `docs/todo/refactor/frontend/restructure/`.

The rejection was explicitly **not** permanent. It named a prerequisite and a trigger:

> Phase 1 is the exact prerequisite a port would need; afterwards it becomes a frontend-only
> project, decidable later on evidence.

## 2026-09-02 — the prerequisite was met

`frontend-reaches-the-backend-through-controllers` reached **0**, with `frontend/app/__init__.py`
named as the single exemption. No frontend module imports a service. The boundary a port would
have to build already existed.

## 2026-09-18 — reversed, in a separate repository

The owner authorised the port. The stated reason is **presentation and customisability** — a
more professional-looking dashboard that is easier to restyle — not a functional defect in
NiceGUI.

What answers the 2026-08-06 objections:

| 2026-08-06 objection | 2026-09-18 answer |
|---|---|
| Node runtime in a Python-only installer | Node is a build-time developer dependency only. The compiled bundle is committed and served by the existing FastAPI process. Installers are unchanged. |
| A second process on the VPS | There is no second process. One uvicorn serves `/api/*` and the static bundle. |
| An HTTP API that does not exist | `backend/src/api/` is that API, and it is thin: it forwards to the controllers the boundary work already produced. |
| 17,842 lines rewritten | Unanswered, and real. This is the cost of the decision, sequenced in `docs/todo/frontend/react-port/`. |
| SSR / CDN / code-splitting do not apply | Still true. That is why the port is **Vite**, not Next.js — a static bundle, no Node server, no SSR. |

The port happens in **`MooreSi/forex-react`**, a separate repository cloned from `MooreSi/forex`
at commit `1d594cb`. The NiceGUI app in `MooreSi/forex` keeps trading and is not modified by this
work. That is what makes the reversal affordable: if the port stalls, nothing that places orders
has been touched.

## What would reverse it again

Two things, either of which is a real signal and not a preference:

1. **The bundle drifts from the API.** A committed build output that nobody rebuilds is a
   dashboard showing yesterday's shape of the data. If a release ever ships a stale `dist/`,
   the packaging decision above is wrong and needs the CI build instead.
2. **The API layer starts making decisions.** The moment a router formats, merges, loops or
   decides whether an order is allowed, the port has recreated in `api/` the pooling problem
   that `history_controller` is the named example of — and the boundary it was supposed to
   inherit is gone.
