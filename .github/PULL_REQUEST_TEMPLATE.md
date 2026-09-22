## What this changes

<!-- What behaviour is different after this, and why. Link the issue if there
     is one. -->

## How you know it works

<!-- Name the test. "Write the test first and watch it fail" is a rule here:
     say what you saw fail and for what reason before the change. -->

## Checklist

- [ ] `.venv/bin/python -m tools.checks all` passes — suite, gates, coverage
      ratchet, boot smoke
- [ ] No test was edited to make this pass (a mock-target relocation is the
      only exception, and it is called out below)
- [ ] No test touches a real broker
- [ ] A `frontend/src` change rebuilds `frontend/dist` in the same commit
- [ ] The domain file under `docs/system/domains/` is updated if this taught
      us something non-obvious

## Does this touch money?

<!-- Order placement, closing, position sizing, the MT5 bridge, the risk
     governor. If yes, say so plainly. It needs the owner's sign-off and a
     demo session before it can merge, however good the change is. -->

- [ ] No, this cannot change what reaches the broker
- [ ] Yes, and I have said exactly which part below

## Anything that came out worse than intended

<!-- A number that regressed, a case you did not cover, something you skipped.
     Say it here. Green output is not evidence. -->
