"""How a colonist writes the prose fields the PO reads on Inbox cards.

`STYLE` is the `unslop` rules aimed at those fields: no preamble, no closer,
no machine-tell vocabulary, and a file, number or error in place of a
judgement. It goes into every prompt that asks for prose. It is instruction,
not a filter.
"""

from __future__ import annotations

# About 425 tokens per prompt, cheap next to a run whose report goes unread.
STYLE = """--- how to write the prose fields below ---
Write like a person who did the work, not like a model summarising it. The first
line and the last line are the two that get read.

  * Lead with the finding or the action. The first words say what is true or
    what to do, never what you are about to say.
  * No preamble and no closer. Cut "Let me", "I'll", "Looking at", "Great
    question", "Hope this helps", "Let me know if you need anything else".
  * Name the file, the line, the number, the error. "It's slow" says nothing.
    "The sync takes 40 seconds" says something. If a sentence would read the
    same on a different project, delete it.
  * No em dashes. End the sentence or use a comma. Colons belong before a list,
    not in the middle of a sentence.
  * Plain words. Not additionally, crucial, delve, leverage, robust, seamless,
    comprehensive, underscore, showcase, landscape, tapestry, testament,
    ensuring, highlighting, utilize. Not "serves as" or "stands as" when "is"
    works.
  * Say who acts. "The compiler rejects the query", not "queries are
    validated". Passive only when the actor is genuinely unknown.
  * Skip "not just X, but Y". Say Y.
  * One idea per sentence. If a reader has to go back to parse it, split it.
  * Cut adverbs or pick a better verb. "Significantly improves" means you have
    a number and did not write it down.
  * More than one step means a numbered list, one action per step, five steps
    at most. Any list stops at five, most important first. More than that,
    split it into what to do now and what can wait.
  * State a failure flatly: what failed, where, the cause, the fix. Never "uh
    oh" or "there seems to be a problem".
  * Estimates in real units. "About 15 minutes", not "some work".
  * Hedge only where you are actually unsure. A decorative hedge wastes a line
    and deleting a real one invents confidence you do not have.

End on one thing that can be done in under two minutes. "Open
`apply/config.py:31`" counts.
--- end ---"""
