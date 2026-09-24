#!/bin/bash
# Attach the private notes repo to this checkout's docs/ folder.
#
# docs/simon-handover/, docs/todo/ and docs/reviews/ are the owner's Q&A, the
# plan packs and the review snapshots. They are needed to work on this app but
# are not for the public, so they live in the private repo
# MooreSi/forex-gold-notes and are gitignored here. This script puts them back
# at the same paths, so every link and code comment that cites them still
# resolves.
#
# The notes repo's git directory is .notes.git/ at the checkout root and its
# work tree is docs/. Two repos share docs/ without overlapping: this one
# tracks docs/system, docs/guides, docs/images; the notes repo tracks the
# three private folders and ignores everything else it can see.
#
# Run once per checkout (Mac, Windows Git Bash, a worktree). Idempotent.
# Afterwards use `git private <cmd>` from the checkout root, e.g.
#     git private status
#     git private add -A simon-handover todo reviews
#     git private commit -m "..."
#     git private push
set -euo pipefail

NOTES_URL="${NOTES_URL:-https://github.com/MooreSi/forex-gold-notes.git}"

cd "$(git rev-parse --show-toplevel)"

if [ ! -d .notes.git ]; then
  git clone -q --bare "$NOTES_URL" .notes.git
fi

notes() { git --git-dir=.notes.git "$@"; }

notes config core.bare false
# Relative to .notes.git, so the checkout can move without breaking it.
notes config core.worktree ../docs
notes config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
# docs/system etc. belong to the public repo; don't list them as untracked.
notes config status.showUntrackedFiles no
notes fetch -q origin
notes branch -q -u origin/main main

# `!` aliases run from the checkout root. Local config only -- git never
# commits aliases, so this is per clone.
git config alias.private '!git --git-dir=.notes.git'

if [ -n "$(notes ls-files)" ]; then
  : # already attached
elif [ -d docs/simon-handover ]; then
  # Folders already on disk (a checkout from before the move): adopt them
  # without overwriting anything; status below shows what differs.
  notes reset -q
else
  # Fresh clone: the folders are absent, write them out.
  notes checkout -q -f main
fi

echo "private notes attached at docs/ ($(notes ls-files | wc -l | tr -d ' ') files)."
notes status --short
