"""How a colonist writes the parts Jordan reads.

Every prose field the colony produces ends up on a card in his Inbox: a
recommendation, a report, an answer to something he asked. The agents were
told what to decide and never told how to write it down, so the shape of the
writing was whatever the model reached for — an opening line announcing what
it was about to say, a closing line asking if he needed anything else, and the
one sentence that mattered in the middle.

This is his own writing-style skill, `i-have-adhd`, aimed at the fields these
prompts ask for rather than at a chat reply. The rules it keeps: the first line
is something he can act on, steps are numbered, lists stop at five, an estimate
is in real units, and nothing opens with a preamble or closes with a
pleasantry. `STYLE` goes into every prompt that asks an agent for prose.

It is a paragraph of instruction, not a filter. Nothing here can stop a model
writing badly; it can only tell it what good looks like, in the same words
Jordan uses on himself.
"""

from __future__ import annotations

# Roughly 1,400 characters, so about 350 tokens on every prompt that carries
# it. That is the cost, and it is worth it: a report he does not read is the
# whole run wasted, and the run costs four figures in tokens.
STYLE = """--- how to write the prose fields below ---
Jordan has ADHD. He reads the first line and the last line. Write so those two
carry the answer.

  * Lead with the action or the finding. The first words say what to do or what
    is true, never what you are about to say.
  * No preamble and no closer. Cut "Let me", "I'll", "Looking at", "Great
    question", "Hope this helps", "Let me know if you need anything else".
  * Name the file, the line, the number, the error. "It's slow" tells him
    nothing; "the sync takes 40 seconds" tells him something.
  * More than one step means a numbered list, one action per step, five steps
    at most. Fold trivial steps into the one before them. A short path he
    finishes beats a complete path he abandons.
  * Any list stops at five items, ranked, most important first. If there are
    more, split them into what to do now and what can wait.
  * State a failure flatly: what failed, where, the cause, the fix. Never "uh
    oh" or "there seems to be a problem".
  * Estimates go in real units. "About 15 minutes", not "some work".
  * No idioms. Write the literal action instead of "circle back" or "get the
    ball rolling".
  * Hedge only where you are actually unsure. Deleting a real hedge invents
    confidence you do not have; keeping a decorative one wastes a line.

End on one thing he can do in under two minutes. "Open `apply/config.py:31`"
counts.
--- end ---"""
