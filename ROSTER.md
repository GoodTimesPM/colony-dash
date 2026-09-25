# The Roster — where colonists come from

Colony Dash does not invent its agents. It **hires** them from a talent pool already on this
machine, then puts them under contract.

## 1. Where personas come from

Two folders are scanned, and **neither is part of this repository**. Both are optional; with
neither, Standby is empty and everything else in the dashboard works exactly as before.

| Folder | What it is | Written by |
| --- | --- | --- |
| `~/.agency-agents` | an optional clone of somebody else's persona library | never, by anything here |
| `~/.colony-agents` | personas you add yourself | the dashboard, and you |

The split is the entire design of this module. The first folder is a **git clone**, and the
next `git pull` in it either clobbers a file written there or refuses to fast-forward past
it — so nothing in Colony Dash ever writes to it. Everything the dashboard creates lands in
the second folder, which upstream has never heard of.

Neither is committed here, either. A persona library is a machine's furniture, not this
project's source; shipping a stranger's agent library inside a repo that merely reads it
would be redistributing their work and would make cloning that library a dependency of
`git clone` rather than of first run.

A local persona **shadows** an agency one with the same `division/filename`. That is the
supported way to override an upstream persona: write your own into the same division and
leave the clone alone.

### Adding one

**Standby → `＋ persona`.** A side panel with two ways in that converge on the same form:
drop a `.md` file and its frontmatter fills the fields, or type them. The division is a
combo box — pick an existing department or type a new one, and it becomes a folder. The
frontmatter is **rebuilt from the fields** rather than passed through, so a file arriving
with a `tools:` or `model:` key loses it on the way in (see §2 for why that matters).

**Standby → `rescan`** re-reads both folders, for a persona added in an editor rather than
here, or an agency clone that has just been pulled.

A persona you wrote gets a **delete** button on its card. An agency one does not, and the
server refuses it too: deleting one would dirty a git clone the user did not think they were
editing, and the next pull would put it straight back.

### The reference library

**[msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents)** is what this
was built against — **270 agent persona files** across 17 divisions, counted by
`python -m colony roster`:

| Division | Files | Division | Files |
| --- | --- | --- | --- |
| engineering | 58 | sales | 9 |
| specialized | 57 | testing | 9 |
| marketing | 36 | paid-media | 7 |
| game-development | 21 | project-management | 7 |
| gis | 13 | academic | 6 |
| security | 12 | spatial-computing | 6 |
| design | 10 | support | 6 |
| finance | 5 | product | 5 |
| healthcare | 3 | | |

(An earlier count said 255 — it missed `game-development/`'s five engine subfolders
(`unity/`, `unreal-engine/`, `godot/`, `roblox-studio/`, `blender/`), which hold 15 real
personas. `strategy/`, `examples/`, `integrations/` and `scripts/` are documentation and
tooling, not divisions, and the scanner skips them.)

A persona file looks like this:

```yaml
---
name: Project Shepherd
description: Expert project manager specializing in cross-functional coordination...
color: blue
emoji: 🐑
vibe: Herds cross-functional chaos into on-time, on-scope delivery.
---
# Project Shepherd Agent Personality
You are **Project Shepherd**, an expert project manager who...
```

Directly relevant to current work: `engineering-backend-architect`,
`engineering-code-reviewer`, `engineering-codebase-onboarding-engineer`,
`testing-reality-checker`, `testing-test-results-analyzer`, `support-analytics-reporter`,
`product-sprint-prioritizer`, `specialized/agents-orchestrator`.

## 2. What they are, and what they are not

I checked the frontmatter of every persona file. Every one of them has exactly five keys:
`name`, `description`, `color`, `emoji`, `vibe`.

**There is no `tools:` key. There is no `model:` key. In any of them.**

That is the whole integration question in one line. These files are **excellent prompts and
zero governance**. They tell an agent *how to think about a problem*; they say nothing about
what it may touch, what it may spend, or which model it runs on. Colony Dash's entire safety
model — read-only investigation, write scope, per-run token ceilings, the three gates — lives
in precisely the fields these files don't have.

So the roster is not a drop-in agent fleet. It's a **hiring pool**: the persona is the
résumé, and Colony Dash writes the employment contract.

## 3. Why not just run `install.sh --tool claude-code`

The repo ships an installer that copies every agent into `~/.claude/agents/`. I'd advise
against running it unfiltered:

1. **Omitting `tools:` means inherit everything.** A Claude Code subagent with no `tools`
   field gets the full tool set. Installing them as-is creates 270 agents with unrestricted
   access — the exact inverse of this project's design. *(Worth verifying against current
   Claude Code behavior before we rely on the inverse either; the safe read is that absent
   permissions are not restrictive permissions.)*
2. **It's global, not project-scoped.** `~/.claude/agents/` applies to every session in every
   folder. 265 entries would clutter agent selection in all your other projects.
3. **Names don't match the convention.** `name: AI Data Remediation Engineer` — spaces and
   capitals, where Claude Code subagents expect lowercase-hyphenated identifiers. The
   installer is a plain file copy and rewrites nothing.
4. **We only need a handful.** Two structural roles plus a few hires cover the current
   backlog. 270 is a catalog to shop from, not a fleet to run.

### 4.1 Hiring is per need, per project

Decided 2026-08-17: **nobody is hired in advance.** No standing roster of "our team" — a
persona is contracted when a specific ticket needs it, scoped to the project that ticket
belongs to, and the contract retires with the work.

The `agents` table enforces this shape: `UNIQUE (role, project)`, and `project` is NULL only
for the two structural roles. So the same persona can be hired twice with different terms —
`backend-dev@job-search/job-radar` with a 120k ceiling and write scope over one folder, and
`backend-dev@colony-dash` with different terms entirely — and neither contract leaks into
the other. Hiring raises a `hire` escalation; it never happens silently.

This is also the honest answer to "which personas should we hire first": none, until a story
needs one. The scan makes all 270 searchable in the dashboard, which is what makes hiring on
demand practical instead of a research project each time.

The installer does support selective install (`--agent`, `--division`, and an
`agents-to-install` list file), which is the mechanism we'd use if we install at all.

## 4. Hiring: how a persona becomes a colonist

```
  .agency-agents/engineering/engineering-backend-architect.md      the résumé
                    │
                    │  hire  ──  PO picks the persona, Ordis writes the contract
                    ▼
  colony-dash/agents/backend-dev.yaml                              the contract
                    │
                    │  compile  ──  persona body + contract + attached skills
                    ▼
  .claude/agents/colony-backend-dev.md                             the employee
```

**Hire** (`colony hire <persona-path> --as <role>`) creates a contract YAML — the shape in
`docs/design.md` §2.1. Ordis proposes `model`, `tools_allowed`, `write_capable`, and
`max_tokens_run` by reading the persona and matching it against similar existing roles; the
PO approves. Hiring is a **PO action**, same class as a write approval. A colonist that can
edit files is a permission grant, and permission grants get a human.

**Compile** generates the actual Claude Code subagent file: proper lowercase-hyphenated name,
an explicit `tools:` allowlist, an explicit `model:`, the persona body as the system prompt,
plus any promoted skills attached. Project-scoped under `colony-dash/.claude/agents/`, prefixed
`colony-` so colonists are never confused with your own subagents.

**Ledger.** An `agents` table records role, source persona path + content hash, contract,
hire date, and rolling stats (runs, tokens, win rate). The hash matters: if you ever `git pull`
the roster and a persona changes underneath a hired colonist, the next pulse flags it rather
than silently changing an agent's behavior.

**Fire.** A role with a bad win rate or repeated PO rejections gets flagged for review. Same
mechanism as skill retirement — the colony prunes what isn't earning its tokens.

## 5. Two hires that aren't like the others

- **`investigator`** and **`reviewer`** are written by us, not hired. They're structural — the
  read-only default and the independent check. They must never depend on a third-party file
  that could change.
- **`specialized/agents-orchestrator`** is a persona for *doing Ordis's job*. We don't hire it,
  because two things picking agents is worse than one. It does have a second-opinion button on
  every pending hire card. Pressed, it reads the story, the picked persona's file and the whole
  roster, then writes one paragraph next to Ordis's reasoning. It cannot hire, cannot reject and
  cannot close the card. The PO still answers.

  It runs when the PO presses it and never on a pulse: a full roster digest is about 18k tokens,
  and most hires don't need one. The two that do are the hire you're unsure about, and the fourth
  contract in a row for the same persona.

## 6. Division → work mapping

Notion `Category` gives a routing hint for which shelf to shop:

| Notion Category | Roster divisions to hire from |
| --- | --- |
| Dev Project | engineering, testing, product |
| Career | *(no good fit — job-search work uses custom roles + `support-analytics-reporter`)* |
| Productivity | specialized, support, project-management |
| AI / Notion | engineering, specialized |
| Gaming | game-development, design |
| Personal | specialized, support |

The Career gap is expected — a 270-persona agency roster has no "help me get an analyst job"
specialist. That work stays on purpose-built roles.
