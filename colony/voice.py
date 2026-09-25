"""How a colonist writes the prose fields the PO reads on Inbox cards.

`STYLE` is the `unslop` rules aimed at those fields: no preamble, no closer,
no machine-tell vocabulary, and a file, number or error in place of a
judgement. It goes into every prompt that asks for prose. It is instruction,
not a filter.
"""

from __future__ import annotations

from .prompt import load

# About 425 tokens per prompt, cheap next to a run whose report goes unread.
STYLE = load("style")
