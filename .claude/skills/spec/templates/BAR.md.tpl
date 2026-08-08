# Screen Bar — <Surface Name>

**Surface:** <Page|Tab|Panel|Dialog|Card|Section>
**File:** <path to the view file that owns this surface>
**Opens from:** <entry point> · <entry point>
**Reads from:** <controller / API route the data comes from>
**Touches money:** <no | YES — which control can place, close or resize a position>
**Status:** draft

> **This is a draft an agent wrote. It is not a bar until you edit it.**
> A bar the builder authored and then grades itself against will always pass. Rewrite the parts that
> are wrong, delete the parts you do not want, then change `Status:` to `agreed`.

## Layout

```
<ASCII skeleton — arrangement only. What is stacked where, what scrolls.
 No colours, no spacing, no font sizes. Show the ordinary populated state.
 [ Button ]   [ Tab ] [ Tab ]   (4) count   • dot   (scrolls)>
```

## Anatomy

One row per part a reviewer would name out loud. Not one row per element.
`Component` is `inline`, `` `<domain>/<Name>` `` for an existing component, or
`` `NEW: <domain>/<Name>` `` for something this work will create.

| # | Part | Component | Holds | Shown when |
|---|---|---|---|---|
| 1 | <part> | `NEW: <domain>/<Name>` | <what is in it> | always |
| 2 | <part> | `<domain>/<Name>` | <what is in it> | `<condition>` |

## States

All five are mandatory. "Cannot happen" is a legal answer — say why.

| State | Trigger | Renders |
|---|---|---|
| Loading | <condition in code terms> | <what the user sees> |
| Empty | <condition> | <what the user sees> |
| Error | <condition — including backend unreachable> | <what the user sees> |
| Disabled | <condition — e.g. trading paused, bridge down, stood down as Remote> | <what the user sees> |
| Stale | <data older than its refresh interval / socket dropped> | <what the user sees> |

## Copy

Every string the user reads, including `aria-label`s and tooltips. Trading terms used here must
match the app Glossary — if a term isn't in it, either reuse an existing one or add a Glossary entry
in the docs task.

| Where | Text |
|---|---|
| <element> | <exact text> |

## Rules

- <hard constraint>
- <If any control here can place, close or resize a position: name the confirmation step, the
  disabled conditions, and what the user sees when the backend rejects it.>

## Out of scope

- <what this surface deliberately does not do, and where that lives instead>
