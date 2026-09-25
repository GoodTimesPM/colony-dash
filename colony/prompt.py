"""The agents' work orders, kept as Markdown under `prompts/`.

A template names its fields `{like_this}` and `render` fills them with
`str.format`, so a literal brace is written `{{`. Templates are read on every
call, so an edit reaches the next run without a restart.
"""

from __future__ import annotations

from pathlib import Path

DIR = Path(__file__).resolve().parent / "prompts"


def load(name: str) -> str:
    text = (DIR / f"{name}.md").read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text


def render(name: str, **fields: object) -> str:
    return load(name).format(**fields)
