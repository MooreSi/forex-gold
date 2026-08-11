---
name: push
description: Push the current branch to origin (never force), verify it landed, and check on CI. Use when the user asks to push, or chained from /commit on "commit and push".
user-invocable: true
allowed-tools: Bash, Read, Grep
---

# /push — Publish the branch to origin

## Steps

### 1. Preflight

```powershell
git status --short          # must be clean — uncommitted work does not travel
git status -sb              # confirm the branch and how far ahead of origin
git log --oneline origin/<branch>..HEAD
```

If the tree is dirty, stop — that's `/commit`'s job first. Read the list of
commits about to publish; if any looks like it isn't this session's work,
say so before pushing.

### 2. Push — plain, never forced

```powershell
git push origin <current-branch>
```

- **Never `--force` / `--force-with-lease` to a shared branch** (hard rule).
- If the push is rejected non-fast-forward, someone else pushed: fetch,
  rebase or merge deliberately, re-run the checks, then push. Do not force
  through it.
- PowerShell quirk: git writes progress to stderr, so a *successful* push can
  surface as a NativeCommandError with a nonzero exit code. Judge success by
  the `old..new  branch -> branch` line and by step 3 — not by the exit code.

### 3. Verify it landed

```powershell
git status -sb    # branch must show level with origin (no "ahead")
```

### 4. CI

Every push runs `.github/workflows/checks.yml` (`tools.checks all` on a
Windows runner). Remind the user to check the Actions tab — a local green
with a red CI is a real signal (environment drift), not noise. While
`docs/simon-handover/readiness-checklist.md` still has an open CI row, a
green run is what ticks it.

## Never

- push with a dirty tree, or force-push a shared branch
- push around a failed local check "to see what CI says"
