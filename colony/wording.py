"""Text the PO reads that runs past a sentence: card bodies, story notes, the
stub a new project starts with.

One-line refusals stay beside the check that raises them, so the rule and its
message read together. Anything longer lives here, where it can be read and
trimmed as copy instead of dug out of the logic. Each is a `str.format`
template; the names in braces are what the caller passes.
"""

# A card body, under the failed command. `why` is one clause, no full stop.
VERIFY_FAILED = (
    "$ {command}\n\n{why}.\n\n"
    "The transcript is on the story, and nothing was accepted on that run. Fix "
    "what it found and dispatch again, or answer here that the criterion no "
    "longer needs the command.")

# A story note when Apply hits a merge conflict.
PATCH_CONFLICT = (
    "The rest of the patch is in your working tree. These files have conflict "
    "markers:\n\n{paths}\n\n"
    "The patch and your uncommitted work changed the same lines. Resolve them, "
    "then press Apply again to close this card.")

# A story note when a patch lands.
PATCH_APPLIED = "Review and commit it yourself. The colony does not commit."
PATCH_UNSTAGED = (
    "\n\nThis one went in unstaged. A file it touches already had different "
    "staged content, so git kept the patch out of the index. `git diff` shows "
    "what landed.")

# The PROJECT.md a folder created from the Inbox starts with.
NEW_PROJECT_STUB = (
    "# {name}\n\n"
    "**Status:** new. Folder created from the Colony Dash Inbox on {day}.\n\n"
    "{why}\n\n"
    "## Next\n\n- Say what this project is for.\n")

# A story note when a delivered story's brief grows while Notion still says
# In Progress.
BRIEF_GREW = (
    "Notion still says In Progress, so this edit is more scope on a running "
    "project. The old criteria are cleared and the next wake re-reads the "
    "page. Nothing already built was touched.\n\n"
    "File the row as Done or Shelved in Notion to stop this.")

# The card body when a groomed story's brief changes.
BRIEF_CHANGED = (
    "Its criteria were written against the older brief, and a story with "
    "criteria is not in the groom queue, so nothing picks this edit up on its "
    "own.\n\n"
    "Reopen it if the edit is new scope. Leave it if you were tidying prose. "
    "This is asked once per version.")
