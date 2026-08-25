# Colony Dash — Master Memory

**Status:** **M0 → M5 shipped.** The loop is live and grooming on the hour, there is a window
to watch it in, that window can *act* — hire, dispatch, approve a patch, halt everything — and
as of 2026-08-18 the link to Notion runs both ways: the colony reads the checklist it is being
judged on, and the PO can set a status, tick a box or leave a comment from the dashboard.
Priority: **High.**
**Created:** 2026-08-15 (as "PO Dashboard"; renamed **Colony Dash** 2026-08-17)

The orchestrator loop and dashboard where Jordan acts as Product Owner over a colony of
Claude agents run by Ordis as Scrum Master. Intended to become the **main dashboard for
Jordan's integration with Claude as a whole** — not a side tool.

- `ARCHITECTURE.md` — the full backbone: state model, pulse mechanics, budget, dashboard, forge.
- `ROSTER.md` — where agents come from (agency-agents) and how they get hired.
- `colony/` — the code. `python -m colony dash` opens the window; `status` is the text view.
- This file — status and decisions only.

## Running it

```
python -m colony init                 create + seed the ledger, scan the roster
python -m colony dash                 open the dashboard window (pywebview)
python -m colony dash --serve         serve only, no window — browse 127.0.0.1:8787
python -m colony status               sprint, board, colony, inbox, pulse log
python -m colony pulse                one heartbeat — free unless it wakes to groom
python -m colony pulse --no-wake      tick only, guaranteed zero tokens
python -m colony pulse --dry-run      preview; spends no tokens, rolls back the ledger
                                      (but still flushes the Notion outbox — see 10.8)
python -m colony roster "database"    search all 270 personas
python -m colony agents               who is on the books and what they may touch
python -m colony sql "SELECT ..."     SELECT-only console
python -m colony mirror               full refresh into MySQL (needs .env + PyMySQL)
python -m colony halt "reason"        stop all spending now; resume lifts it
python -m colony allowance 10         +10 points of week for a high-volume sprint (0 clears)
python -m colony projects             what moved across the whole tree (--diff PROJECT)
python -m colony shortcut             (re)write the desktop shortcut + icon
python -m colony schedule             (re)install the hourly pulse task (--show, --remove)
```

Ledger: `colony-dash/.colony/ledger.db` (gitignored). Config: `colony-dash/.env` (gitignored)
holds `NOTION_TOKEN`, `NOTION_DATABASE_ID`, and `COLONY_MYSQL_*`.

## What this is

The in-practice execution of the Notion idea **[Mimic Scrum Environment](https://app.notion.com/p/3a6e98f012738047bccdf1671a127948)**
(Priority: High, Status: In Progress). That page holds the *why*; this folder holds the build.

- Jordan = **Product Owner** — owns backlog, priority, approvals.
- Ordis = **Scrum Master** — the heartbeat loop that grooms, staffs, dispatches, reports.
- The colony = **specialist agents** — hired from the roster, spawned per ticket, stateless,
  they die after one run.
- SQLite ledger = the continuity. Notion = where intent is planned.

The name: the agents are a colony with different functions, and the skill **forge** is where
what they learn gets made permanent.

## Reference studied

**Lloyd**, from u/croovies' r/ClaudeAI post "Example of a real working loop orchestrator"
(431 upvotes) — [analysis](https://explainx.ai/blog/claude-code-loop-orchestrator-heartbeat-ticket-memory-august-2026).
Runs on scape.work (Mac-only), so we rebuild the pattern rather than adopt the product.

Four things taken from it: heartbeat pulses that log even when clean; SQLite as the memory
instead of the context window; read-only investigation before any write; escalation as a
proposal, not a decision. The Colony panel is a direct descendant of Lloyd's Sessions panel.

## Decisions locked

### 2026-08-15
- **Hermes is parked.** Claude Code alone drives this. Revisit only if always-on delivery
  becomes the bottleneck.
- **Skills are a first-class subsystem**, not a byproduct. Learned procedure is mined from
  agent runs, drafted into real `SKILL.md` files, and promoted back to the colony *and to
  Ordis*. Memory files hold facts; skills hold procedure. Do not conflate them.
- **Dashboard is a local read-only web app**, not a TUI. The only write control is PO
  approval in the Inbox.
- **Three human gates:** grooming, writing, skill promotion. Everything between is autonomous.
- **Write-capable runs execute in a git worktree.** The patch is what surfaces for approval.

### 2026-08-17 — the PO answered the four open questions
- **Renamed PO Dashboard → Colony Dash.**
- **Cadence: hourly**, resolved with a **two-tier pulse**. A "tick" is pure Python and costs
  **zero tokens**; it only wakes Ordis when something actually changed. Idle hours are free,
  which is what makes hourly affordable. (`ARCHITECTURE.md` §4.0)
- **Budget unit is TOKENS, not dollars.** The USD figure still renders, grayed, beside it.
  On a Pro plan nothing is metered in money, so tokens are the only honest unit.
- **Sprint budget = the 7-day usage window.** Sourced from the *existing*
  `personal-desktop-projects/token-usage-in-tray` app, which already reads
  `api.anthropic.com/api/oauth/usage`. Colony default: **35% of the week**, leaving the rest
  for interactive work. Amber at 30%, dispatch stops at 35%.
- **One usage poller, not two.** That endpoint allows ~5 req/5 min per account and Claude
  Code shares the bucket. The tray app stays the only poller and writes a shared cache file;
  Colony Dash reads the file. (`ARCHITECTURE.md` §6.2)
- **Blast radius: all of `D:\ALL STUFF\PROJECTS`, read.** Write is only ever the one project
  folder named by the active ticket, in a worktree, after approval. Never `.env`, never
  `.git/` internals, never outside the directory, never `push`/`amend`/force-push.
  **Priority weight to `job-search/` and the Job Radar pipeline.**
- **Notion is the intake.** The [Project Ideas/To-Do](https://app.notion.com/p/1d23280aadd341cbbd1467c771ee6d88)
  board is where the PO posts intent from anywhere. **`Status = "In Progress"` is the trigger
  to work on a row**; the page body is the brief. Thin briefs go `needs-info` and raise an
  Inbox card + a Notion comment naming the specific missing decision — the colony never
  guesses. (`ARCHITECTURE.md` §5)
- **The escalation bar is approved as written** (`ARCHITECTURE.md` §4.6), to be tuned against
  real pulse logs.
- **agency-agents is a hiring pool, not a fleet.** 255 personas at `C:\Users\jtbal\.agency-agents`.
  None of them carry `tools:` or `model:` frontmatter, so they supply persona and zero
  governance. Do **not** run `install.sh --tool claude-code` unfiltered. Hiring is a PO
  action. (`ROSTER.md`)
- **SQLite stays**, over the local MySQL server: no service that can be down when the pulse
  fires, the ledger travels inside the project folder, one writer only, same dialect the
  Notion MCP already speaks. Optional nightly mirror into MySQL for reporting/practice if
  wanted. (`ARCHITECTURE.md` §3.3)
- **Build the dashboard, don't adopt one.** FastAPI + SSE + one HTML page, wrapped in
  **pywebview** so it's a real desktop window. Grafana/Metabase can't do the approval gate;
  Retool/Appsmith still need our backend and add a hosting dependency; Streamlit is the
  fallback fast path. The published artifact already *is* the front end. (`ARCHITECTURE.md` §9.1)

### 2026-08-17 (later) — M0 built, three questions closed
- **No `Project` property on the Notion board.** The board stays a plain idea board; the
  colony adapts. Ordis infers the folder by matching story text against the real directory
  tree two levels deep (`job-search/job-radar`, not `job-search`) and confirms once via the
  Inbox. **An inference never earns write scope** — `project_source` must be `confirmed`
  first. (`ARCHITECTURE.md` §5.2)
- **MySQL mirror: yes, and built.** `python -m colony mirror` full-refreshes the ledger into
  MySQL for reporting and SQL practice. One-way; SQLite stays the system of record; nothing
  in the colony reads back. (`ARCHITECTURE.md` §3.4)
- **Hiring is per need, per project.** No standing team. `agents` is keyed
  `UNIQUE (role, project)` so the same persona can hold different contracts on different
  projects. Nobody is pre-hired. (`ROSTER.md` §4.1)
- **Roster is 270 personas, not 255.** The earlier count missed `game-development/`'s five
  engine subfolders. `python -m colony roster` is the authority now.
- **Four UI features taken from the Lloyd sidebar:** per-agent avatars (deterministic pixel
  sprite from `agents.avatar_seed`, tinted with the persona's own color, emoji as badge),
  an active/standby colony rail whose progress bar is tokens-against-ceiling, a searchable
  standby browser over all 270 personas grouped by division, an artifacts panel (Lloyd's
  file manager, ours being run patches and reports), and click-through story detail built
  from the `story_events` timeline. All are backed by tables that now exist.
  (`ARCHITECTURE.md` §9.3)
- **The tray app now publishes the usage cache** to `%LOCALAPPDATA%\claude-usage\usage.json`
  (atomic replace, best-effort). It must be **restarted once** for this to take effect.

## Build order

**M0 Ledger ✅** → **M1 Pulse ✅** → **M2 Dashboard ✅** → **M3 Hiring + gates ✅** →
**M4 Skill forge ✅** → **M5 Two-way Notion ✅**. Details in `ARCHITECTURE.md` §10.

The build order as designed is complete. What comes next is not another milestone — it is
using the thing: answering the Inbox, pointing a write-capable ticket at a real story, and
promoting the first skill the forge has already found.

**M0 is done and verified:** 11 tables plus an FTS5 roster index, hash-checked append-only
migrations, structural agents seeded, 270 personas scanned, the CLI, and the MySQL mirror.

**M1 is done.** The tick samples usage, syncs Notion, infers projects, writes the
`story_events` timeline and decides tick-vs-wake — all at **0 tokens**. The wake tier now
spawns a real read-only agent to groom a story: it either drafts acceptance criteria and
parks the story at `po-review` for your approval, or names the one decision only you can
make. Guards: HALT, the 35% weekly allowance, a per-run ceiling, 2 stories per wake, and a
2-attempt cap per story. `--no-wake` forces a free tick; `--dry-run` writes nothing.

**Live cost, measured:** one grooming run ≈ **50k chargeable tokens ≈ $0.50**. See
`ARCHITECTURE.md` §10.2 — the first wake found a genuine bug in `job-radar/score.py`
unprompted, and cost about half a dollar to do it.

**M2 is done.** `python -m colony dash` opens a real desktop window (pywebview over a
localhost-only FastAPI server) with every panel from `ARCHITECTURE.md` §9.2: the sprint band,
the colony rail with deterministic avatars, the board with click-through story drawers built
from `story_events`, the PO Inbox, the pulse log, the forge, and spend. Live updates arrive
by SSE. It is **read-only by construction** — every request opens the ledger with
`read_only=True`, so the window cannot be the reason state changed. The approve buttons came
with the M3 gate.

**M3 is done.** The dashboard can now change state — through exactly one module. Six gates,
four of them human: groomed → PO accepts the criteria → project confirmed → agent hired with
a write scope → PO dispatches → build runs in a worktree → **PO approves the patch**. The
write contract grants Edit/Write inside one throwaway worktree and **denies Bash outright**,
which is what makes "the colony cannot push" a capability statement rather than a promise.
Approved work lands **uncommitted** in the real folder — Jordan reads the diff in the drawer
and commits it himself. Nothing in `control.py` spends tokens: approving marks a story
dispatchable, and the next wake decides, so a mis-click is free. Every write records a
`po_actions` row in the same transaction as the effect it authorises.

Also shipped with M3, from the same session's asks:

- **HALT and the allowance boost** as one-click macros in a new side rail. HALT writes both
  `.colony/HALT` and a `controls` row, never stops the heartbeat (it keeps logging, syncing,
  reaping), and honestly promises **"no new work"** rather than implying it can kill a run
  in flight. The boost is stored separately from the sprint baseline and capped at
  **+25 points**; clearing it is the same call with 0.
- **The file-manager view.** `projects.py` runs one `git status` for the whole tree and
  buckets it by longest path prefix; the Projects panel lists what moved and a drawer shows
  the real diff. The pulse logs **movement, not dirtiness** — only folders whose counts
  differ from the last sample or that have commits in the window, because a log that repeats
  "75 untracked" every hour is a log nobody reads.
- **A broadened pulse log.** Each beat now carries a `detail` blob, and clicking a beat opens
  what actually happened that hour.
- **Ordis has a panel.** Beats, wakes, tokens spent, anomalies, what's groomable, what's
  queued, what's running, and the last thing it said — the loop is now a visible member of
  the colony instead of an invisible narrator.
- **Standby is browsable.** 270 personas as collapsible division dropdowns, and clicking one
  opens the **actual persona file from disk** — description, identity, mission, and every
  critical rule, parsed into sections. Read on click, never carried in the snapshot: what you
  see is what the agent gets handed, not a copy that drifted.
- **Its own face.** `icon.py` draws a lit longhouse with Pillow at nine sizes (no more
  sharing the Balatro mod manager's Python icon), `shortcut.py` writes a desktop `.lnk`
  pointing at `pythonw.exe`, and the window sets its taskbar icon via `WM_SETICON` because
  pywebview's `icon=` is GTK/Qt-only.
- **Eight themes** in a header dropdown — system, light, dark, ember, moss, slate, parchment,
  clay — persisted in localStorage.
- **The PO Inbox is a full-width tile strip**, and each tile now offers only the affordances
  its kind can actually use: a `needs-info` card asks for a folder and *doesn't* show
  approve/reject, because naming the folder is the only answer it has. **An In Flight rail
  sits to its right**: open tickets and unsent Notion pushes in one scrolling list, because
  every control on this page queues rather than acts and nothing on screen said so between
  the click and the next pulse. A tile whose story has work in flight carries an *in flight*
  badge; hovering it lights the matching rows, and hovering a row lights the tile.
- **Drawers keep a trail.** Opening a project from inside a beat used to be a one-way trip;
  the header now grows a back button naming where it returns to. Views that cannot honestly
  be restored — the composer, holding a half-written reply — end the trail instead.
- **The Files list sorts by changes, recent, or name**, remembered in localStorage. *recent*
  reads file mtimes rather than commit dates, because these rows are uncommitted work and the
  last commit says nothing about when it happened. Commit rows in the project drawer show
  their time in a column of their own.
- **An appearance drawer**, off the header, holding three dials and nothing the server sees:
  **text size** as a multiplier on the whole type scale (a flat +2px would collapse the scale,
  so every step carries the same `--ui-scale`); the **fifteen palette tokens** as colour
  pickers, each showing the contrast ratio it owes and going coral when it breaks; and
  **tile layout** — which column a panel lives in, its order there, and an optional height cap
  that makes the tile scroll inside itself with its title bar pinned. *Randomize* rolls the
  neutrals but keeps the four accents inside their hue bands, because a "coral" that came out
  green stops saying anomaly; every colour has its lightness solved for the contrast it owes.
  Presets save the palette, the base theme and the text size together and are named.
- **The Files panel says what "modified" is measured against** — one commit,
  named in the heading and spelled out in the drawer, whose eyebrow no longer
  reads "undefined · undefined". Buckets are named in English, and each row
  carries when it was last written.
- **The pulse stopped claiming decisions were waiting on you.** It was counting
  every escalation you had ever decided, forever; it now counts the window, and
  says they are decisions you *made*.
- **"Projects that moved" is a real list** — what changed since the previous
  beat, which files, when they were written, and whether the colony wrote them.
  Migration 012 stores the deltas that make that possible.
- **The spend window moves.** `«` / `»` page it, the date box jumps it to a
  day, `now` comes back to the present, and with the chart focused the arrow
  keys walk it a bucket at a time (Home/End, PageUp/PageDown too).
- **Panel headings no longer print `u25BE`.** It was a JavaScript escape in a
  CSS rule; the caret is gone rather than fixed.
- **The Inbox ghost slots stay out to the Ticket Queue.** They were padded from
  a column count measured before the grid had been laid out.
- **Spend is a real chart now** — hour / day / week / month / year, line or
  bar, gridlines on both axes, and the date and figure under the pointer.
- **Learnings keep their whole text.** They were being cut at 400 characters
  on the way into the ledger with nothing kept behind them; long ones now fold
  with a *show the rest* button. The twelve already recorded stay truncated.
- **Click a panel's title bar to fold it away.** It shrinks to the title bar
  and stays folded across restarts. Click again to open it.
- **The token allowance goes anywhere from 0% to 100% of the week**, in ±5
  steps or by typing the number. It used to stop at 60% and only climb.
- **The story timeline is colour-coded like the reply drawer** — amber you,
  violet Ordis, mint a learning — and names the rows the same way.
- **The reply drawer has a back button** to the story it came from.
- **A story that is ready to start says so in the Inbox.** Its own mint tile,
  with a *dispatch to build* button — and, when dispatch would refuse, the
  reason on the tile instead of behind the press. Nothing had ever been
  dispatched because nothing ever announced that it could be.
- **A conversation belongs to the story, not to the question.** Opening *reply
  to Ordis* on a re-raised question used to show an empty thread while the real
  history sat under a closed escalation. It shows the whole exchange now.
- **The thread shows questions and learnings too**, colour-coded: amber is you,
  violet is Ordis, mint is what he learned, and the dim line is the question
  that started that stretch.
- **The token allowance moves in ±5 steps.** It used to only go up.
- **You can highlight and copy text.** The pywebview shell defaults
  `text_select` to False and enforces it with `user-select: none` across the
  whole document, so nothing on the page could be selected. It is on now.
- **Replies to Ordis take screenshots and files.** Paste into the composer, drop
  onto it, or use *attach a file*. They land in `.colony/attachments/`, show as
  thumbnails in the thread, and reach the agent as a path in its work order.
- **Any story can be replied to**, from a button in its drawer — no Inbox item
  required. Saying something about a story used to mean waiting to be asked.
- **"In Progress" is a status you can set from the dashboard**, and dropping a
  story offers *Shelved* rather than *Archived* — which is not an option on the
  Notion select and was making every drop-with-push come back refused.
- **A filed story keeps its title.** The shelf was truncating it into a dim stub;
  it wraps now and clicks through to the story.
- **Marking a story Done in Notion now actually reaches the colony.** The sync
  had been filtering its Notion query to *In Progress OR Exploring*, so a row
  moved to Done vanished from the result set instead of being seen to move — the
  whole of the filing work below could never fire. The sync reads the entire
  board now, and skips the body fetch for rows it may not act on so the cost is
  unchanged.
- **The status buttons file the story immediately**, and queue the Notion push as
  the mirror. Waiting for the round trip meant the tile stayed loud for up to an
  hour, and forever if Notion writes were off.
- **Replying to Ordis puts a ticket in the queue.** It used to be created, run
  and closed inside one wake, so the reply visibly went nowhere. The ticket now
  opens when you send the message, carries what you wrote, and says
  `waiting for Ordis` until the wake claims it.
- **Done, shelved and not-started are a real state now.** Five of the seven Notion
  statuses are the PO *filing* a row, not asking for anything — they set
  `stories.settled_as`, leave the working status untouched, close open questions as
  moot, and move the story to a **filed** shelf under the Board that toggles like the
  dropped one. Before this, every status that wasn't *In Progress* mapped to
  `needs-criteria` and was therefore groomable, so an idea written down and left alone
  came back an hour later as a question in the Inbox.
- **Answering a groom question no longer freezes the story.** A blocked groom ticket is
  a receipt; it was outliving its question, showing as a duplicate BLOCKED tile *and*
  spending the story's last of two grooming attempts. Spent tickets are retired every
  tick, and confirming a project folder gives the attempts back.
- **The pulse log scrolls** inside its own body, and keeps 120 beats — about five days.
- **The header says when the last run ended**, and a queued skill draft says what it is
  held behind. A frozen token count and an idle forge both read as broken when the real
  answer is that the week is over its allowance.
- **Appearance survives a relaunch.** pywebview defaults to a private WebView2
  profile, so every localStorage key — theme, text size, palette, tile layout —
  was binned when the window closed. The profile now lives at `.colony/webview/`.
- **In Flight is now the Ticket Queue**, and the PO Inbox scrolls at 46vh rather
  than wrapping downwards, padding its last row with hollow slots out to the
  queue so the strip keeps its shape however far behind you are.
- **The palette editor is grouped and labelled by what each colour paints** —
  surfaces, text, the four meanings, twelve panel titles and four git states.
  The titles and git states are tokens of their own, mixed from the accents but
  pinnable one at a time, so "make the Ordis title greener" no longer also
  repaints Standby, Board and the modified-files bar. All sixteen clear 3:1 on
  panel across the twenty-five shipped theme variants.
- **Snap** turns the whole board into a drag surface: the drawer closes, every
  tile wiggles, and a drop inserts rather than swaps.
- **Relative times tick every fifteen seconds.** "last beat 0m ago · next in 60m" used to be
  baked at render and frozen until the next SSE push — an hour of a clock reading *younger*
  than the truth, on the one panel that exists to show the loop is alive.

What guards the write door: every `/api/act/*` POST must carry an `X-Colony: 1` header (a
cross-origin form can POST to localhost but cannot set a custom header), the server binds
127.0.0.1 only, and `control.Refused` maps to **409 with the message intact** — every refusal
names the state and the next move.

## Next

- [ ] Answer the Inbox. Five stories are parked on "which project folder?" and
      "15 Part Job Search" is parked on a real decision: should the manual *Job & Internship
      Tracker* and the auto-written *Job Radar Tracker* merge, coexist with a defined
      handoff, or one retire? **Check the stale flags first** — the next pulse will mark any
      question the Notion page has already moved past, and a stale card wants **re-ask**
      rather than an answer.
- [ ] **Approve or reject the first hire Ordis picked himself.** Inbox card #19: Developer
      Tooling Engineer as `scan-cli-builder` on `personal-desktop-projects`, for
      *Full computer scan*. Rejecting is a real answer — the next pulse proposes someone
      else, and the counter that keeps the roster diverse learns from it either way.
- [ ] **Restart the dashboard window** to pick up 014 through 019. The ledger is already
      migrated; the running window is serving the older code.
- [ ] **Story #1 is back in the groom queue.** The card was retired — delivered stories now
      resume on their own — and "15 Part Job Search" is sitting in *needs criteria* waiting
      for a wake to re-read the brief you grew. File the Notion row as Done when you want
      that to stop.
- [ ] Watch the first sprint roll. Sprint 1 now ends Friday 2026-08-28 05:00; the tick that
      crosses that instant closes it and opens Sprint 2 with no goal set, which is a
      deliberate blank for the PO to fill.
- [ ] Let it run a week, then tune the escalation bar (§4.6) against real logs.
- [ ] **Promote the first real skill.** Three candidates are waiting in the FORGE panel;
      none has been drafted yet, because drafting costs tokens and that is the PO's call.
- [ ] **First live write-capable ticket.** M3 is verified against a copy of the ledger; it
      has not yet been pointed at a real story end-to-end.
- [ ] **The manual is frozen on purpose.** `file` → `manual` in the header opens a
      snapshot of the vocabulary, the lanes, the four gates, and the troubleshooting list,
      dated 2026-08-22. It reads nothing from the ledger and is not maintained as the code
      changes. When the workflow settles, delete `MANUAL` in `index.html` and write it
      again from the code rather than patching it a line at a time.
- [ ] Update the published artifact — it still shows the pre-M1 design.

## Finished 2026-08-25 — a panel for work that is already done

"I want there to be a "completed dispatches" or "completed stories". That way i
can keep track of progress and check on work that has been done so i dont
accidentally work on the same thing just cause i forgot we worked on something."

Every panel on this dashboard was about what is happening or what needs a
decision. Nothing on it answered "what has this colony actually produced", and
the closest thing — the Board's `filed` toggle — answers a different question:
where a story stands, not what came out of it. A story can be filed with three
deliveries behind it or none, and the toggle shows the same row either way.

**Completed** is the middle column's third panel now, under the Board. Two kinds
of thing end and both are in one list sorted by the date they finished:

| kind | what it is | when it is added |
| --- | --- | --- |
| dispatch | an `implement` ticket that reached `done` | when its ticket closes |
| story | a story filed `done`, `shipped` or `shelved` | when Jordan files it |

A dispatch is listed whether or not its story is finished, because the patch it
delivered is the thing you would otherwise rebuild by hand next week. A story
filed `not-started` is not listed at all: nothing happened, and a record of
finished work that includes work nobody began stops being worth reading.

The part that makes it a record rather than a list of dates is the window. Each
entry covers the ground from the *previous* delivery on the same story to this
one, not from the moment its ticket was cut. Most of what happens around a
dispatch — the questions, the answers, the criteria being argued over —
happens before the ticket exists, so an episode that started at `created_at`
would leave out the reason the work was done. `_episode_window` in `server.py`
picks the boundary; `_episode` merges `control.conversation` with the
`story_events` that are not message mirrors, and returns them in one order.

The tile carries counts and the drawer carries text. Counts, because the list
rides in the snapshot that every panel shares and text does not belong there:
messages, questions, blockers, learnings, runs, tokens, dollars. Clicking a tile
opens `/api/completed/detail`, which is where the work order, the findings, the
skip list, the runs table, the tickets cut in the window and the whole
conversation live.

Nothing on this panel writes. It reads `tickets.status`, which a ticket sets when
its own run closes, and `settled_as`, which is Jordan's word for a story and
stays his.

## Finished 2026-08-24 — the lane a story fell into, and Ordis reading its own pulse

Two blockers stood on story #1 at once, and neither was what it said it was.

The first was a false alarm the loop generated about itself. Ordis, answering a
PO reply, read `.colony/pulse.lock` and the tail of `.colony/pulse.log` and
reported pid 21968 as a stuck process holding the lock. That pid was the 22:07
pulse — the run Ordis was executing inside. A lock file with a live pid in it
and a log entry with a header and no result are exactly what a healthy pulse
looks like from the inside, halfway through. `reply_prompt` now states its own
pid and says plainly that the newest pulse entry and the lock are itself, and
that it must never report the current beat as hung or ask for it to be killed.

The second was real and had been swallowing decisions since M5. When a PO reply
settles a question, the story comes out of `needs-info` and used to go straight
to `backlog`. But `GROOMABLE_WHERE` only reads a `backlog` story while its
acceptance criteria are empty, and a story that had already been groomed still
had them. So it landed in a lane nothing reads: not groomable, not dispatched,
no ticket, no card. It sat there until Jordan asked why nothing happened.

There are three lanes now, and the one a story takes turns on whether its
criteria were ever approved:

| story on unblock | lane | why |
| --- | --- | --- |
| no criteria | `backlog` | never groomed; grooming finds it there |
| criteria, never approved | `needs-criteria`, criteria cleared | drafted without the answer he just gave |
| criteria he approved | `ready` | his approval stands; the question only interrupted it |

The third row is the one that matters. Clearing criteria Jordan approved would
make him approve the same list a second time, so the story goes back to the lane
the question interrupted and waits for Dispatch, which is his call and stays his
call.

One more thing came out of the same trace. `decisions_since` used
`resolved_at > since`, where `since` is the previous pulse's `pulse_at` — and
`pulse_at` is stamped during the tick, before the wake runs. Every decision the
wake itself resolved landed on that exact second and was excluded from the next
window forever. It is `>=` now. The 20:53:52 decision was invisible to the 21:07
beat for precisely this reason.

Story #1 was repaired by hand to match: escalation #31 resolved, the story back
in `ready` with its approved criteria, and a timeline note saying why.

## Finished 2026-08-24 — the colony writes the way Jordan reads

Jordan enabled the `i-have-adhd` writing-style skill for his own sessions and
asked for the colony to use it too. The agents had always been told what to
decide and never told how to write it down, so the shape of a recommendation
was whatever the model reached for: an opening line announcing what it was
about to say, a closing line offering further help, and the one sentence that
mattered in between.

`colony/voice.py` holds `STYLE`, a block of about 1,400 characters that states
the rules against the fields these prompts actually ask for. The first line
carries the answer. No preamble and no closer. Name the file, the line, the
number. More than one step means a numbered list of at most five. A failure is
stated flatly with its cause and its fix. Estimates are in real units. End on
one thing he can do in under two minutes.

It goes into all five prompts that ask a colonist for prose — grooming, the
build work order, the hiring decision, an Ordis reply, and a skill draft — and
in each one it sits immediately above the JSON contract, so the last thing the
agent reads before the field list is how to fill the fields in. That costs
about 350 tokens per prompt. It is worth it: a report Jordan does not read
wastes the whole run, and a build run costs four figures.

Nothing here can stop a model writing badly. It can only say what good looks
like, in the words Jordan uses on himself.

## Finished 2026-08-24 — the checkout an agent gets, and a way to hand work back

Four things, all from the same run. Story #1's build agent skipped three of six
criteria and its report was cut off mid-sentence on the card.

**The report is not cut off any more.** The escalation's `recommendation` was
written with `[:2000]`, so a card that had more to say ended at "LOOK C". SQLite
has no length limit, so the cap bought nothing. It is gone, along with the caps
on a ticket's findings and on the text of a reply to Ordis. The one-line
headline fields keep a limit, but `build.clip` now cuts them on a word boundary
and marks the cut, so a shortened line never reads as a finished one. On the
card the report is a `longText` block: folded to a few lines with a button
saying how many characters are behind it, the same as everywhere else on the
page. It used to be an 8.5em box with its own scrollbar inside the scrolling
inbox.

**The worktree holds what is on disk, not just what is committed.** This was
the real cause of two of the three skipped criteria. `create` checked out HEAD
and stopped, so nine files Jordan had staged in `job-search/assisted-apply` and
not committed were absent, and the agent reported truthfully that
`PROPOSAL_next_steps.md` does not exist. `worktree.seed` now copies in every
tracked file that differs from HEAD repo-wide, plus every untracked file inside
the agent's scope folders, and records the result with `git write-tree`. The
diff is taken against that tree rather than HEAD, so Jordan's own uncommitted
work does not come back in the patch as though an agent had written it. No
commit is involved: a tree object is not a commit and master is untouched. One
untracked file over 2 MB is skipped and the work order says how many were.

**A contract can be allowed to read the credential files.** `.env` is git-ignored
in every project here, so a worktree never contained one, and an agent asked
whether `NOTION_API_KEY` is set answered that it is not — which was wrong, and
was the second thing Jordan asked about. `agents.sees_secrets` is off by
default; the contract drawer has a switch for it. When it is on, `seed` copies
the `.env` files from the top-level project containing each scope folder, after
the base tree is written, and `diff` deletes them before it looks. A key
therefore cannot reach a patch even if the agent edits the file. What the switch
cannot stop is an agent repeating a value in its report, which is why it is a
decision and not a default, and why the work order tells the agent to name the
key and never the value.

**A build agent can hand over a command instead of skipping the criterion.**
The third skipped criterion was "run `py -m apply.main auto` and confirm the OG
tracker row appears", and a build agent has Read, Grep, Glob, Edit and Write and
nothing else. It now writes the command into `needs_run` and the colony raises a
`run-request` card carrying the command, the criterion it answers, and what a
correct result looks like. Approving runs it in the project folder with a
90-second limit, puts the whole transcript on the story as a finding, and sends
the story back to `ready`. It does not move to `accepted` and it does not touch
Notion: the command answered a question, and what that means for the story is
Jordan's decision. `colony/runner.py` refuses `git push`, `git commit`, a forced
git operation, a recursive force delete, a pipe into a shell, and anything
outside the projects directory — a refused request is recorded on the story
rather than put in front of the PO.

**And the next agent reads what the last one found.** `build.history` puts the
story's findings, learnings and decisions into the work order, details included,
oldest first. That is what makes a run-request worth raising: the output of the
command comes back as a finding, and the next build reads it instead of asking
again.

**One thing the runner fixed on the way past.** Migration 023 rebuilds
`escalations` to widen a CHECK, and `tickets` and `po_messages` both hold a
foreign key into it, so the drop failed at COMMIT. `PRAGMA defer_foreign_keys`
does not help: it counts violations rather than re-checking them, and a
`DROP TABLE` raises a count that recreating the parent never lowers, so the
commit fails while `foreign_key_check` reports nothing wrong. `db.migrate` now
runs each migration with foreign keys off, as SQLite's own procedure for this
says to, and runs `foreign_key_check` afterwards. That check is new: before,
a migration could leave a dangling reference and nothing would say so. 019 did
the same rebuild and got away with it.

Not done, and worth saying: nothing here lets agents talk to each other
directly. They pass work through the story, which is the only place a decision
is recorded. Live ledger backed up to `.colony/ledger.pre023.bak` before 022 and
023 were applied.

## Finished 2026-08-24 — a write scope the PO can change

**The scope is on the contract now, and the contract drawer can edit it.** A
write scope used to be one folder, derived once from the project an agent was
hired on, with nothing in the dashboard able to change it afterwards. Open a
write-capable agent and its drawer shows the folders it may edit as chips, a
dropdown of every folder the colony knows about, and a box for typing one that
is not in the list. Save writes the contract and records a `scope` action, so
the change reads back in the log like every other one.

**Why it had to change.** Story #1 was filed under `job-search/assisted-apply`,
and three of its six criteria were about `job-search/job-radar` — the OG-tracker
sync, a PROJECT.md checkbox, and a live run. The build agent skipped all three
and said why: "a project I have no write access to — my scope is
job-search/assisted-apply only". That was the contract working correctly. The
missing piece was any way for the PO to widen it short of retiring the agent and
hiring it again.

**What the change does not do.** The agent keeps the project it was hired on.
`dispatch` still matches a story to an agent by that project and
`build.contract` still looks the contract up by it, so widening a scope is not
the same as moving an agent. The server checks each folder exists under
`D:\ALL STUFF\PROJECTS` before storing it, refuses `..`, absolute paths, dot
folders and an empty list, and refuses a read-only contract outright — there is
no write scope to widen on one.

**The work order names every folder.** `build_prompt` listed one path under
WRITE SCOPE; it now lists the contract's folders, one per line. Migration 021
adds `scope` to the `po_actions` verbs, the same rebuild 020 did.

**A worktree is git's copy of the last commit, and the work order now says so.**
The other half of the same story: the agent reported `NOTION_API_KEY` and
`NOTION_JOBS_DB` as unset when both are set in
`job-search/assisted-apply/.env`. `.env` is in that project's `.gitignore`, and
a worktree is checked out from HEAD, so the file is not in the checkout at all.
`claude -p` also runs with the worktree as its working directory and no
`--add-dir`, so the real `.env` is out of reach from there too — which is the
behaviour we want for a credential file. What was wrong was the report: an agent
that cannot see a setting was saying the setting is not set. The prompt now
states that untracked and uncommitted files are absent, and that an absent
`.env` means invisible rather than unset.

## Finished 2026-08-23 — a beat you can ask for

**Migration 020: both buttons returned 500 until `po_actions` learned the verb.**
Every control writes itself down before it acts, and `force_pulse` passes
'pulse' as the action name. The CHECK on `po_actions.action` did not list it, so
the INSERT failed, the endpoint returned 500 and the dashboard showed
"refused (500)". No beat ran, because the thread starts after the record. 020
adds 'pulse' to the list and nothing else. The check that shipped with the
feature called `pulse.run` directly and never went through `force_pulse`, which
is why it passed on something that could not work.

**Pulse now, and tick only, in Macros.** The heartbeat was a Windows scheduled
task and nothing else: if you wanted the colony to look at the world you waited
for :07. There are two buttons now. "Pulse now" runs a full beat, wake included,
and "tick only" runs the free half — sync, reap and look, but never spend. HALT
and the allowance still apply, so neither button is a way around them; they only
ask the question sooner.

**The schedule does not move.** The task fires at :07 whatever the dashboard
does, so a beat forced at 1:37 sits between the 1:07 and the 2:07 beats rather
than replacing either. The strip reads `next_pulse_at` off the newest pulse row,
and writing now + an hour there would have made a forced beat lie about when the
next automatic one is due, so a forced row carries forward whatever the last
scheduled beat promised. The row is stamped `forced` in its `actions` JSON, the
pulse log labels it, and it is kept out of the "clean ×6" rollup — you asked for
it by hand, so you get to see that it happened.

**One pulse at a time.** This was already a hole: the scheduled task, the CLI and
the dashboard are three separate processes, and two pulses running together
would sync Notion twice, reap the same orphaned runs twice, and could dispatch
the same story twice. Every entry point now takes `control.pulse_lock()`, a
plain O_EXCL file in the runtime directory, and the second arrival raises
`control.Busy` and stands down rather than queueing. A lock older than 35
minutes is treated as abandoned, because the scheduler kills its own task at 30.
The CLI prints "stood down" and exits 0, so a collision does not read as a
failed scheduled task.

**The request does not wait for the beat.** A pulse takes seconds when it is
clean and minutes when it wakes, which is far too long to hold an HTTP request
open, so `control.force_pulse` records the action, starts a thread with its own
connection and returns "started". The button reads "beating…" while the lock
is held. A beat that dies on the thread appends its traceback to the same
`pulse.log` the scheduled task writes, because a forced beat that failed
silently is exactly the complaint the control exists to answer.

## Finished 2026-08-23 — a card cut at 1000 characters, and a confirmation nobody could make

Three faults, found from one screenshot of story #1's reply drawer.

**The card was cut off in the middle of a word.** The banner ended "The weekly
command runs and pr". Every card's text was stored through a `[:1000]` — eight
sites in `wake.py`, one in `pulse.py` — and story #1's acceptance criteria are
1359 characters, so the PO was being asked to approve a list whose last two
bullets he could not see. Nothing was wrong with the display; the text was
already gone by the time it reached the page. The cap is now
`control.card_text`, which keeps 8000 characters, cuts at a word if it ever has
to cut at all, and puts an ellipsis there so a cut is visible as a cut. Story
#1's live card was repaired from `stories.acceptance_criteria`, which had kept
the whole thing. `_event` shortens a summary with `_gist` now instead of
slicing at 400, for the same reason.

**Ordis confirmed something he had no way to check.** He wrote "NOTION_OG_TRACKER_DB
is set in .env.example with the database ID — it's live now, not just logging
'not set'." The key is set, but in `.env`, which is outside his read scope; he
read the committed template beside it and reported one as the other. "It's live
now" he could not observe at all — his contract denies Bash, so he cannot run a
program and cannot see one run. The next groom then wrote acceptance criteria on
top of both claims. `reply_prompt` now states plainly what Read, Grep and Glob
can establish and what they cannot, forbids the words live, running, working,
fixed and verified, and names the `.env` / `.env.example` trap. The reply JSON
gained a `checked` list: the files he actually opened to support `settled`. If it
comes back empty, `answer_po` records the settled line as "Jordan says: ...
(Ordis opened no file to check this.)", so the agent downstream reads it as
somebody's word rather than as a finding.

**The story said "blocked" while it waited on an approval.** Story #1 was
`po-review` with criteria drafted and `blocked_reason` NULL, and the drawer
still showed the coral "it cannot start until this is answered" banner. One
`needs-info` card from that morning was still open, and one open card of that
kind is what paints the banner. Nothing closed those cards on the happy path D
only the branch that raised a *new* blocker superseded the old one. The new
`control.clear_needs_info` runs at the two points a story stops being blocked:
criteria drafted, and a PO reply that puts it back in the groom queue. Story
#1's stale card was closed by hand; its drawer now reads amber, "waiting on your
decision".

## Finished 2026-08-23 — where the blocker sits, and when things happened

Three changes to what the drawers and the Inbox show, all from the same reading
session.

**The Inbox card no longer quotes Ordis's last answer.** It used to print the
most recent thing Ordis said under the card's own text. That answer is often to
something said days earlier, so it read as part of the question the card was
asking and pushed the actual ask out of view. The reply button already carries
the message count and the whole exchange is one press behind it. The `last_reply`
subquery came out of the snapshot query with it — nothing renders it now, and it
was pulling a full message body into every poll.

**The blocked banner moved to the bottom of the conversation.** In the reply
drawer it was pinned above the proposal and above every message, which put it as
far from the box you type in as it could get. On a thread with ten messages in it
the one line saying what is holding the story was the line you had to scroll back
up to find. It is appended to the end of the thread now: the conversation runs
oldest to newest, where the work stands is the newest thing in it, and it sits
directly above the textarea. The colours and the four levels are unchanged.

**Tickets and runs carry dates.** The story drawer listed `#19 research done ·
investigator` and a run table with no clock in it, so neither list could answer
whether a row was from this morning or from three weeks ago — the ids only give
the order. Tickets now show when they were opened and, if closed, when they
closed. The runs table gained a `started` column and a `took` column, the second
being `ended_at - started_at`, which is the number worth having when a run cost
more than expected. The agent drawer's run table gained `started` too. A new
`stamp()` helper prints month, day and time; the existing `when()` prints a
weekday and is still right for things inside the next few days.

## Finished 2026-08-23 — a patch that half applied and said it had not

Applying the patch from ticket #54 printed `the patch would not apply:` followed by five
files applied cleanly, a three-way merge, and `error: job-search/assisted-apply/apply/main.py:
does not exist in index`. Every part of that report was misleading.

`apply_patch` ran `git apply --3way --check` and then `git apply --3way`. The `--check` pass
was there to make the apply all-or-nothing, and it does not do that: with `--3way` it reports
success when a merge is *possible*, not when it is clean. The real apply then merged five
files into the working tree, hit a conflict on `apply/main.py`, wrote conflict markers into
it, left stages 1/2/3 in the index, and exited non-zero. `control._settle_patch` caught the
non-zero exit and reported a refusal. So the PO was told nothing had landed while six files
had changed under him, one of them with conflict markers in it. The `does not exist in index`
line came from a second press of Apply: once a path is conflicted it has no stage 0, and
`git apply --index` looks for stage 0.

`apply_patch` now tries a strict `git apply --index` first, which really is all-or-nothing,
and only falls back to `--3way` when strict refuses. A `--3way` failure is checked against
`git diff --diff-filter=U`: an empty list means nothing moved and it is a real refusal, and a
non-empty list raises the new `worktree.PatchConflict` carrying the paths. `_settle_patch`
catches that separately, keeps the card open, and writes a story event naming the conflicted
files and saying plainly that the rest of the patch is already in the tree.

### The reason there was a conflict at all

`worktree.create` branches from `HEAD`. The PO's working tree carries staged, uncommitted
work almost all the time — nine files under `job-search/assisted-apply/` had been sitting in
the index for days — and the agent never saw any of it. It built against HEAD, so its patch
described a file that no longer existed anywhere except in git.

Both sides of the conflict were real work. The patch added `discord` to the import line in
`apply/main.py`; the uncommitted tree had added `notion_sync` to the same line. Both modules
are called in the merged file, so both imports were kept. The second conflicted line was
byte-identical to HEAD on the patch's side, meaning the patch never touched it and the
overlap was incidental, so the tree's version stood. Ticket #54's card was closed as approved
by hand rather than by pressing Apply again, because a second Apply re-runs the same merge
against the same HEAD base and would have overwritten the resolution with markers a second
time.

This will happen again on every story whose project folder has uncommitted work in it, which
is most of them. The fix is to seed the worktree with the live tree's uncommitted state for
the ticket's write scope, record that state as the diff base, and take the agent's patch
against it. That is a change to how patches are produced and it has not been made.

## Finished 2026-08-22 — the DONE badge, and delivered stories that keep going

The board printed DONE on "15 Part Job Search" as soon as the patch from ticket #22
was approved. The PO had not said the project was finished, and did not want the loop
deciding that for him:

> "just because I finish one part of the project does not mean I am completely finished
> with the project ... running projects are the norm for this type of work ... once I
> added more info into the notion project folder, I want that to be taken as more info to
> the same project to continue production."

There were two faults behind the one symptom.

The first was the label. `accepted` means the PO approved a patch and the files landed in
the working tree uncommitted, and `index.html` mapped that status to the word *done*. The
status that actually means finished is `settled_as`, which only a Notion status or the PO's
own button can set. Story #1 did not have it: `settled_as` was NULL and the Notion row still
read In Progress. So the board was reporting a decision nobody had made. That lane now
reads **delivered**.

The second fault was that the code behaved the way the label read. A story in `accepted`
has acceptance criteria, so `wake.GROOMABLE_WHERE` skips it, and it is not `ready`, so
nothing dispatches it. There was no way out of that status except by hand. Editing the
Notion page produced `1 changed` in the log and no other effect, so a project that was
still being worked on had become invisible to the loop meant to work on it.

`pulse.resume_delivered` gives that status a way out. When a delivered story's brief
changes and its Notion row still says **In Progress**, the check clears the acceptance
criteria, returns the grooming attempt budget, and moves the story back to *needs criteria*
for the next wake to re-read. It does not raise a card first, because on a running project
more scope is expected rather than unusual. The check itself costs nothing: no model runs
in the tick, and the re-read is an ordinary groom on the next wake under the usual budget.

Two conditions keep it safe. The Notion row must still say In Progress, so a row filed as
Done or Shelved, or moved to Exploring, does not resume. And the page hash must have
changed, which the sync's own list of changed ids guarantees.

The `brief-changed` card from earlier today still applies to the other statuses. When a
story is *ready*, *running*, or *po review* and its brief moves underneath it, that is worth
asking about, and clearing the criteria during a build would throw away work in progress.
`BRIEF_CHANGED_WHERE` now excludes `accepted`, so the card and the resume never fire on the
same story. The one card already raised was retired, since the new rule answers it.

### What the colony may set, and what it may not

> "I don't want you to change statuses of projects cause that changes how you read these
> projects."

The colony may move a story through the **working** lanes — `needs-criteria`, `backlog`,
`ready`, `in-progress`, `po-review`, `needs-info` — because that is the loop reporting where
work is. It may **never** put a story in a lane that reads as finished (`accepted`,
`archived`, any `settled_as`), and it may never push a Status to Notion. Ending things is
the PO's, in Notion or on a button, and nowhere else.

## Finished 2026-08-22 — what the words mean, and the edit that went nowhere

**"wake" was being used for two different things.** `tier` records what the tick *decided*;
it does not record what happened. An hour stamped `wake` because the tick found a reason,
whose wake then read its job list and stood down, was drawn in mint next to a token count
of zero, above a body that said "spent nothing — the heartbeat is free". Thirty-eight of
the fifty-four `wake` rows in this ledger are that shape. The schema keeps its two tiers,
because two is the right number and a third would only move the ambiguity; the page asks
`acted` instead, computed from `actions.wake` in the record rather than sniffed out of the
finding text, and labels those hours ticks — escalated. `wake skipped:` became
`wake stood down:`, because skipping is a failure and standing down is the loop working.

The vocabulary, settled, since it kept drifting:

| word | what it is |
|---|---|
| **pulse** | the scheduled hourly event. One `pulses` row, every hour. A missing row is the alarm. |
| **tick** | the free half. Pure Python, no model, zero tokens. Decides whether the hour is worth spending on. |
| **wake** | the paid half. Ordis runs. Only when the tick found a reason *and* there is a job to do. |
| **groom** | one job a wake can do: turn a brief with no acceptance criteria into criteria and questions. |
| **beat** | prose for "one pulse". Not a concept; nothing in the schema knows the word. |

**A brief that changes after grooming now raises a card.** `GROOMABLE_WHERE` excludes any
story that already has acceptance criteria, deliberately: re-grooming an accepted story
every time a sentence is fixed would pay a model to reproduce an answer we already have.
The edge went unwritten. Once a story is groomed, built and accepted, editing its Notion
page does *nothing* — the tick reports "1 changed" in the log and drops it. Story #1 had
new scope added to it and two consecutive beats reported a story edit and then stood down,
because the queue that would have picked it up excludes exactly that story.

`brief-changed` is free: the tick raises it, no model reads it, and `raised_hash` holds it
to once per version of the brief. Approve clears the criteria *and* the attempt budget and
puts the story back in the groom queue; reject records that the edit was cosmetic and
changes nothing; the x dismisses it until the page moves again. Whether new prose on a
built story is new scope or a tidied sentence is a judgment the tick cannot make and a
model should not be paid to guess at hourly — so the loop asks.

Migration 019 rebuilds the `escalations` CHECK to admit the word. SQLite cannot alter a
constraint in place, so the table is copied by name and both indexes are recreated.

## Finished 2026-08-21 — an x, a ceiling, and the patch you can read

**The ceiling was never measured.** 120,000 was the default in `propose_hire`,
written before the colony had ever built anything, and it went unquestioned
into every write-capable contract. The first real build spent 209,233
chargeable tokens on a nine-file change and came back correct — so the
ceiling was not protecting a budget, it was manufacturing a cost escalation
about a run that worked, which is the 002 lesson repeating on a different
column. 400,000 now, roughly twice the one measured build: headroom that still
catches a runaway without punishing a large honest one. Read-only roles keep
their smaller numbers, because grooming and reviewing are bounded work and
those figures came from real runs.

**The Inbox has an x.** Every control on a tile was an answer to the question
— approve, reject, later, re-ask — and the only one that could clear a tile
without answering was "drop story", which takes the whole story off the board,
cancels its tickets and closes its other questions. The cheapest way to tidy
the Inbox was the most destructive thing in it, and a stale question about a
problem already solved elsewhere had no exit that did not cost something.

`dismiss` is not a fifth decision, and 017 is a column rather than a widened
CHECK for that reason: the PO gave none of the four answers. `po_decision`
stays NULL — the same shape `settle` already uses to close a filed story's
questions as moot — and `dismissed_at` says which moot this was. The story is
untouched: same status, same criteria, same place on the board.

Dismissal needed teeth or it would have been a button that does nothing.
`ensure_blocked_visible` runs every tick and re-raises a card for any blocked
story that has lost one, so without a second clause the very next beat would
have put the card straight back. And `control.question_settled` now answers for
both raise paths in `wake`: a question is settled while its card is open, and
also while it stands dismissed **against this version of the brief**. Edit the
story in Notion, the hash moves, and Ordis is allowed to ask again — which is
right, because by then the answer might have changed. Not on a write approval:
there is a patch on disk and a worktree behind it, and closing that question
without answering it strands both. The server refuses it; the UI just does not
draw the button.

018 came out of the same work. `po_actions.action` has a CHECK listing three
decisions, and `_record` passes the decision straight through as the action
name — so `amend` has never been writable either. `control.decide(esc,
"amend")` accepts the argument, gets as far as recording it, and dies on a
constraint. Nothing calls it that way yet, which is the only reason nobody has
seen it. Both values go in.

**The patch drawer stopped telling you to open a file in another program.** At
the one gate where reading before deciding is the entire point, it showed the
escalation's two sentences and a grey box reading "the patch is on disk at the
path above". `/api/patch` now assembles the four things no single source has:
what the build claims (the ticket's findings JSON — which criteria it met,
what it skipped, what it wants looked at), what the diff actually moves
(counted here, off the patch itself, because a summary of a different artifact
is a summary you cannot check), what it cost, and the patch text. Per-file
counts sort by churn, because nine files that each gained a line is a different
review from nine files each half rewritten and the totals cannot tell them
apart. The decision sits above the file list rather than under a thousand lines
of diff: scrolling to the bottom to approve assumes you read all of it.

Verified against the real escalation — 9 files, +664 -59, matching
`git diff --stat` exactly.

## Finished 2026-08-21 — faces, and a week that exists

**Every hired agent has a face.** The assisted-apply integrator was a blank square and the
two structural agents were not, which looked like a missing feature and was a colour bug.
Canvas does not throw on a colour it cannot parse — it ignores the assignment and leaves
the previous fill standing, and the previous fill was the panel background. So the sprite
was drawn, faithfully, in the background's own colour. Seven of the roster's colour words
are Tailwind names CSS has never heard of; every persona wearing one would have come out
blank, and the structural pair escaped only because they have no roster row at all.

`asHex` asks canvas whether it understood, using two sentinels, because a rejected
assignment is visible only as the absence of a change. Valid is not the same as visible,
though: nine roster colours are near-black, and `#000000` on a sunk dark panel fails the
same way by a different route. `legible` walks lightness until the tint clears 3:1, taking
its direction from the **panel** rather than from the colour — `toward` reads it off the
colour, which is right when the colour was chosen for a known background and wrong for 270
of them written by people who never saw this one. All 270 clear 3:1 in light and dark.

**The token figure is live, and the week is the real week.** Two complaints, one cause.

The dashboard read the newest row of `usage_samples`, and only the hourly pulse writes
those — so a figure the tray app refreshes every five minutes could be fifty-five minutes
old, and old in the way that is hardest to see, because a percentage that has not moved is
exactly what a quiet week looks like. `usage.read()` now runs on every snapshot. It is the
same file the pulse copies from, so this is not a second poller and earns nobody a 429; the
ledger rows stay as history and as the fallback for a machine where the tray app has never
run. `stale` is on the strip now, because that is the only thing that can tell a quiet week
from a dead tray app.

And the allowance week resets **Friday at 05:00 local**, which the cache has been reporting
all along in UTC — the strip was printing the first sixteen characters of that string, so
a window closing at five in the morning read as *08:59*. Sprint 1 was seeded with today + 7
as an admitted placeholder, with a docstring promising the pulse would correct it. It never
did. For four weeks a Monday-to-Monday sprint has been measured against a Friday-to-Friday
budget, and `date(started_at) BETWEEN starts_on AND ends_on` counted the five hours before
one reset and the whole day after the next: eight days of runs against a seven-day
allowance, double-counted at both seams.

`usage.week_window` is the one place that knows where the edges are. It prefers the reset
instant the API reported, because that is the truth and the Friday-05:00 arithmetic is only
a model of it — a constant cannot know about a daylight-saving shift or an account whose
window moved, and the reported instant can. `pulse.align_sprint` moves the sprint onto those
edges each tick and **rolls** it when the window turns: the old sprint closes, the next
opens, no goal carried over, because a sprint that silently extends past its own budget week
is a budget that does not exist. Migration 016 gives `sprints` the instants; the dates stay
as their human shadow. Every spend query is half-open on timestamps now.

One consequence worth expecting: **the sprint total dropped to zero.** It read 877.4k
tokens across a Monday-to-Monday window that had already reset once. The week that actually
started this morning at 05:00 has no runs in it yet.

## Finished 2026-08-21 — the four things the loop was making the PO do

Four complaints from the PO seat, all of them the same complaint: the loop kept handing
Jordan work that was its own.

**Images pasted into the chat are read now.** They always could be — attachments live on
disk and go to agents as absolute paths, and a live `claude -p --allowedTools Read` describes
one correctly. But only `reply_prompt` ever mentioned them, and only the ones on the message
being answered; `groom_prompt` and `build_prompt` mentioned them nowhere. Story 1 carries
three screenshots, and the groom that asked Jordan for “the exact field list ... it was
never transcribed into text anywhere” was holding a prompt that did not know they existed.
**It asked him for a picture he had already sent.** `attachments.for_story` collects every
file on a story's thread and `attachments.evidence` puts them in all three prompts.

**Blockers moved into the conversation.** `/api/thread` returns a `state` block — whether
the story can move, what stops it, every open ask on it — and the composer draws it above
the proposal: coral for blocked, amber for waiting on you, mint for moving. Mint is drawn as
loudly as coral on purpose. A banner that only appears when something is wrong teaches you
to read its absence, and an absent banner looks exactly like a panel that failed to load.
Open questions inside the thread are coloured by kind too; they had all been the same grey
rule, so six asks gave no clue which one was still holding the story.

**Every answered escalation writes a ticket.** Approvals, rejections, deferrals, project
confirmations, dispatches — “any call/choice can be made a ticket to ensure that it has
been understood” — with the question as the work order and the answer as the findings.
Twelve backfilled. `tickets.intent` has a CHECK that SQLite will not widen in place (014's
header records the two table rebuilds that failed and exactly why, so nobody retries them),
so a decision is `intent='chore'` plus `decided_esc_id`, the same shape a reply already had
as `intent='research'` plus `po_message_id`.

**And the PO stopped picking agents.** The tile that said “open Standby, pick a persona and
hire them with write scope” was asking him to read a roster of 270 people to do the Scrum
Master's job. `wake.staff_stories` picks one per pulse for a ready, project-confirmed,
writer-less story, reads the persona files and the project, names the finalists it passed
over, and brings a name to approve. `control.propose_hire` and the `hire` escalation kind
had existed since M3 with nothing calling them; this is the caller they were waiting for.

The roster goes in **unfiltered** — all 270 personas, 69k chars. FTS-filtering it by the
story text *is* the bias: a search over the story can only ever return personas whose
description already sounds like the story. And bias gets a counter rather than a rule,
because the thing that would produce it is the same thing you would be asking to police it.
`roster.times_hired` / `last_hired_at` are shown in the prompt, and the diversity line
attached to each proposal — first contract or Nth, and whether every finalist came from
one division — is **written by Python**, not by the agent's own account of its reasoning.

It worked on the first pulse. Story 5 got **Developer Tooling Engineer**, first contract in
this colony, finalists spanning engineering and security, chosen off the acceptance criteria
rather than the title: it passed over the incident responder because “malware-flagged files
are one line item in a routine optimization pass, not an active breach.” That is the
distinction the PO was being asked to make from a list of job titles.

**Cost to know:** 74k chargeable tokens for one hiring decision, most of it the roster
digest. If that proves too rich per hire, the lever is the digest's `desc_chars`, not a
filter over it.

Two things broke on the way and are fixed: `control._event` takes the four columns it was
written for, and a hire is the one event that also wants `ticket_id` and `tokens` (it
inserts directly now, and run #31's event was backfilled rather than paying twice); and
`"approve" + "d"` is `"approved"` while none of the other three are, so the timeline had
been saying **PO rejectd** for as long as it has existed. `_PAST` maps all four and 015
repairs the rows.

## Finished 2026-08-18 — M5, the link that runs both ways

Three complaints, one bug: **the colony had no memory of which version of a story it was
talking about.** The Inbox kept asking about work already finished, there was no way to change
anything from a phone, and a story you had decided against sat on the board forever.

- [x] **The checklist has two halves now.** `fetch_page_content()` keeps ticked and unticked
      to-dos apart (`stories.done_items` / `open_items`) and **both** feed the content hash, so
      ticking a box in Notion is a change the colony notices. The board shows `4/7`; the story
      drawer lists what is done; and the grooming prompt opens with **ALREADY DONE (n) — treat
      these as closed, do not re-raise them, do not ask about them** before it says what is
      still open. That last part is the whole fix for tiles asking about last month: the
      information simply was not in the prompt.
- [x] **Questions can go stale.** `escalations.raised_hash` records the version of the story a
      question was written against; when the story moves on, `stale_at` is stamped. Stale cards
      drain of amber, sort to the back, hide behind a toggle, and carry **re-ask** — which
      resolves the question as amended, puts the story back in the grooming queue and voids the
      groom attempts the old pass used up, so the colony is actually allowed to try again.
      Nothing is deleted: the colony really was confused, and that is worth being able to read.
- [x] **The PO can write upward.** Set a status, tick a to-do, leave a comment — from the story
      drawer, on a phone-readable page, without opening Notion. `control.py` still performs no
      network I/O: the button writes a `notion_outbox` row and the next tick sends it. That buys
      retries, an audit trail of everything the colony has said upward, and a switch that holds
      the queue rather than dropping it. `flush()` never raises — a tick that dies because
      Notion was slow is a tick that stops doing the eleven other free things it was going to do.
- [x] **The Notion vocabulary is deliberately tiny.** Status, checkbox, comment. No creating
      rows, no deleting rows, no editing the brief — the brief is unambiguously the PO's.
      `"In Progress"` is not writable, because it is the status the intake filter selects on and
      a loop that can write it can feed itself work forever.
- [x] **`notion_write` is a separate switch from HALT.** HALT means *spend nothing*, and a
      comment is not a token. A halted colony that also went mute upward looks broken rather
      than paused.
- [x] **Stories can be dropped**, from the Inbox, the board row, or the drawer. It archives
      rather than deletes, refuses to proceed without a reason, closes the open questions,
      cancels the waiting tickets, optionally sets the Notion page to Archived, and stays
      restorable. It refuses outright on an `in-progress` story: a running ticket has a worktree
      and a budget attached, and archiving out from under it orphans both.
- [x] **Eighteen more themes**, in three families — eras (diner, miami, y2k), worlds
      (cyberpunk, neon noir, grid, vault, imperial, nostromo, bridge, meadow) and terminals
      (phosphor, ochre crt, dusk, fjord, kiln, blueprint, ink wash). Twenty-four in total,
      grouped in the picker. Every one keeps the same four jobs for the same four accents —
      amber is the Inbox, mint is live work, coral is an anomaly, violet is Ordis — because that
      rationing is what lets a glance mean something.
      **Trimmed the same day, after the PO looked at them together:** the first pass shipped
      thirty-six, and eleven of them were the same theme. Deco, cold war and grunge were all
      moss with a different comment; harvest, clay, paper, rivendell and outer rim were all
      parchment; spice, frontier and mordor were all ember. They are gone, because a picker with
      three of everything is a picker you scroll rather than choose from. Four more were rebuilt
      rather than removed — **diner** is now the sign at 1am (neon red tubing, blue gas, black
      glass) instead of a beige lunch counter, **y2k** is the rave flyer (violet, lime, orange,
      hot pink on black) instead of frosted chrome on white, **imperial** is black, white and
      red with no hue anywhere else, and **meadow** got its sage mixed at pigment strength. The
      rule the trim taught: a theme has to be *nameable from its colours alone*, or it is a
      duplicate wearing a costume.
      **Then the same rule was run over the twenty-four that were left**, and seventeen of them
      were rebuilt from their reference rather than from "dark ground plus a hue": cyberpunk
      leads with the acid yellow it is actually known for; neon noir is the same street seen
      through rain, deliberately *less* saturated than its neighbour; miami's ground is the
      purple the sky goes rather than navy; bridge quotes the LCARS palette outright; fjord
      quotes the polar-night one; vault gets the rust and the warning lamp that separate a
      machine from phosphor's bare tube; phosphor and ochre crt are strictly one hue at four
      intensities; ink wash desaturates three accents to tinted greys so the vermilion anomaly
      is the only saturated thing on screen; ember picks up the blue at the base of the flame;
      dusk gets the rose horizon back. Every palette is contrast-audited in the same pass —
      ink/ground at 4.5:1 and every accent at 3:1 against its panel. Fjord failed it (the
      published aurora red scores 2.46) and its coral is lifted two steps, because an anomaly
      colour you have to look for is not an anomaly. **Daylight and basalt were left alone on
      purpose:** they are what a viewer who never opens the picker sees, and the neutral
      default is their identity.
- [x] **Section titles have colour.** Eleven headings in `--ink-dim` read as one grey mumble.
      The hues are derived from the four accents with `color-mix`, so every palette
      get them without drifting out of key, and the 3px bar down the left of each title is the
      part that actually carries at 11px mono.
- [x] **A view menu.** Every panel can be hidden, stale questions and dropped stories toggled,
      rows made dense — persisted in localStorage and **sent nowhere**. Hiding a panel does not
      stop the colony filling it. Panels default on, detail defaults off, and nothing is ever
      hidden without a count in the header saying so.
- [x] **The folder dropdown is alphabetical.** It fell back to the working-tree scan, which is
      ordered by what moved most recently — right for "what did I touch today", useless for
      finding a folder. Sorted at the point of render, so it holds whichever list fills it.

Verified on a **copy** of the ledger: `"In Progress"` and an empty comment both refused; a
flush with writes held queued nothing; two escalations marked stale and the story un-parked back
to `backlog`; the same hash marking nothing on the second pass; re-ask resolving as `amend` and
refusing twice; drop closing 2 questions and refusing to repeat; restore clearing `dropped_at`;
`groomable` falling to 0 with the story dropped; and the progress section rendering ALREADY DONE
(2) / STILL OPEN (1). Migration 008 is applied to the live ledger; `done_n`/`open_n` fill in on
the next sync.

## Finished 2026-08-18 — M4, the skill forge

The compounding loop. A memory is a fact; a skill is a procedure — the forge only makes the
second kind, and only when the ledger already proves the procedure exists.

- [x] **Detection is free, so it runs on every tick.** `forge.detect()` is pure SQL over runs
      already paid for, so it lives in the free tier rather than the wake: a HALTed colony
      should still notice that a procedure is emerging, because noticing is not doing. Four
      signals — the same ticket class solved 3+ times, a ticket that failed and then succeeded
      (the recovery path *is* the lesson), a run that came in at or under half its class
      median, and the PO correcting the same kind of thing 3+ times. A ticket class is
      (intent, role), not title: a skill is a procedure for a kind of work.
- [x] **It found three real ones on the first pass** against the live ledger:
      `research-investigator-procedure` (5 clean runs, 31,944-token median),
      `research-investigator-shortcut` (run #10 came in under half that), and
      `po-correction-defer` (double-digit defers — the colony keeps raising something the PO keeps
      putting off, which is a procedure problem).
- [x] **Drafting is queued, not clicked.** `control.py` never spends tokens, so "ask for a
      draft" writes `draft_requested_at` and the next wake pays, behind the same budget guard
      as grooming. The draft may come back `worth_it: false`, which retires the candidate with
      its reason — the cheapest place a bad idea can die is before a file exists.
- [x] **Promotion is the third human gate**, and the only path in the whole system that writes
      a file to disk: `PROJECTS/.claude/skills/<slug>/SKILL.md`, plus an attachment to named
      roles. `ordis` is a legal role with no `agents` row — that is the "learned from the
      colony, handed to the Scrum Master" path. The drawer shows the entire draft before the
      button, because approving a procedure you have not read is what a gate exists to prevent.
- [x] **Measured in the budget's own currency.** `skill_uses` records one row per run that
      loaded a skill, with the baseline it was judged against, so `tokens_saved` can always be
      taken apart into the runs behind it. Savings may be negative. A skill loaded into a run
      that then failed counts as a loss, recorded before the early returns.
- [x] **Decay is flagged, never auto-retired.** Active skills under a 50% win rate on 5+ uses
      surface as `decaying`; the PO retires them. Retiring detaches the slug from every
      contract and keeps the row, because the long-run question is which *detector* keeps
      proposing failures.
- [x] `python -m colony forge --detect / --draft ID`, four gated API routes, and a real FORGE
      panel: status-coloured cards, the detector that proposed each one in plain words
      ("seen 3+ times", "you kept fixing it"), and what the active ones have earned.

Verified end-to-end on a **copy** of the ledger with the skills directory redirected into a
scratch tree: promote wrote the file and attached to `ordis` + `investigator`, the preamble
reached the work order, one run recorded **19,944 tokens saved** against a 31,944 baseline,
retire detached the slug everywhere, and `skill_path("../../evil")` raised. On the live
server: no `X-Colony` header → 403; promoting a candidate → 409 *"only a drafted skill can be
promoted"*; queueing the same draft twice → 409. Test residue was cleared from the live ledger
afterwards, so no wake spends tokens on a request the PO did not make.

## Finished 2026-08-18 — M3.1, the PO's own quality-of-life pass

Ten things Jordan asked for after living with M3 for a day. All shipped.

- [x] **No console windows, ever.** Two separate bugs wearing one costume. The flashing
      2-3x/minute was `PROJECT_TTL_S = 30.0` in `server.py`: every 30s the SSE snapshot
      re-ran `projects.scan()`, which shells out to `git` from a GUI process. The hourly
      window that never closed was the scheduled task running `cmd.exe`. The fix needs
      **both halves**: `colony/proc.py` wraps every subprocess in `CREATE_NO_WINDOW` +
      a hidden `STARTUPINFO` (kills the children — git, the claude CLI, WScript.Shell),
      and `colony/schedule.py` reinstalls the task under **pythonw.exe** (kills the
      parent). Suppressing one alone leaves the other on screen. `python -m colony
      schedule --show` reports `windowless` when both halves hold.
- [x] **Reply to Ordis.** Every Inbox card has a `REPLY TO ORDIS` button that opens a
      screen-wide composer with the proposal pinned above the box — the same thing as
      typing into Claude Code, but from the tile. Migration `005_po_replies.sql` adds
      `po_messages`; `control.reply()` queues a row, **spends nothing**, and leaves the
      escalation open. The next wake reads it (`wake.answer_po`, before grooming) and
      answers. A reply is not a decision: Ordis may *suggest* a project from one but never
      confirm it — `project_source` stays `inferred`, so a reply can never authorize a
      write (§8.2). An unanswered message stays `unread` so the question survives.
- [x] **New folders from the Inbox.** The project picker now carries a `+ new project
      folder...` option. `control.create_project()` whitelists each path segment
      (fails closed) *and* re-checks the resolved path. This was the "Full computer scan
      (Optimization)" case — no existing folder fit, and the dropdown had no answer.
- [x] **Dropdowns fit their tile** at any window size (`select.pick { max-width:100%;
      min-width:0 }` — the missing `min-width` was what let the grid child overflow).
- [x] **"Later" now moves something.** It writes `snoozed_until` (8h default); the tile
      dims and sorts last (`ORDER BY snoozed, ...`). Un-snooze is the same call with
      `snooze_hours: 0` — note `or 8` is wrong there, 0 is falsy. "story" is now
      `OPEN STORY`, which is all it ever did.
- [x] **The dark themes actually apply.** Pure CSS specificity: the system-dark guard
      `:root:not([data-theme="light"]):not([data-theme="parchment"])...` scored 0,4,0 and
      beat `:root[data-theme="ember"]` at 0,2,0, so on a dark OS every named dark theme
      lost to system-dark. The guard is now `:root:not([data-theme])` — a chosen theme is
      never "system", so the *absence* of the attribute is the whole condition.
- [x] **Parchment re-saturated** into real sepia (`--ground:#E4D5B7`, panels tinted rather
      than white-with-a-hint, accents as pigments: ochre / verdigris / vermilion).
      A sepia theme whose panel is `#FBF6EB` is a white theme standing near a candle.
- [x] **A real file tree** in the middle column. `scan()` answers *what moved*; `tree()`
      answers *what is there* — a panel built on `scan()` looks empty exactly when the
      tree is tidy, which is why the old one looked unimplemented. Lazy, one directory per
      request (the root has ~60 projects, some with `node_modules`). `safe_path()`
      validates the **resolved** path, so a symlink out of the tree fails like a `..`, and
      rejects hidden names and anything matching `is_secret()` (`.env*`, `credentials.json`,
      `id_rsa`, ...). Verified: `/api/tree?path=../../` -> 400, `/api/file?path=.../.env` -> 400.
- [x] `?theme=<name>` forces a theme for one load without touching localStorage.
- [x] `pulse.cmd` deleted — the task no longer references it.

## Finished 2026-08-17

- [x] Notion integration connected 2026-08-17. The board reads: **6 rows, 6 stories** —
      3 backlog (In Progress), 3 needs-criteria (Exploring), 5 Inbox cards awaiting a project.
- [x] Tray app restarted 2026-08-17 21:10 — `usage.json` is publishing; the pulse now reads
      `5h 32% · 7d 18%` from it.
- [x] Hourly Task Scheduler job **"Colony Dash Pulse"** registered 2026-08-17. Runs
      `pythonw.exe -m colony pulse` (interactive user, on battery too, `StartWhenAvailable` so a missed hour
      fires on wake), appending to `.colony/pulse.log`. Verified: exit code 0.
- [x] Wake-tier dispatch built and run live 2026-08-17. Four bugs it exposed — the token
      ceiling counting cache reads, a budget breach discarding paid-for work, the wake never
      firing for work already in the ledger, and duplicate structural agents — are written up
      in `ARCHITECTURE.md` §10.2 and migrations 002/003.

### Bug fixed 2026-08-17: inference matched source folders, not projects

First contact with the real board sent "Run Hermes Agent alongside Claude Code" to
`balatro-mod-loader/build` and "Mimic Scrum Environment" to `balatro-mod-loader/data`. Two
causes, both now closed:

1. **Candidate list was every child directory.** `build/` and `data/` are source folders, and
   generic names collide with ordinary English forever. `candidate_projects()` now counts a
   folder as a project only if it holds a **`PROJECT.md`** — the master CLAUDE.md's own
   definition. 15 real candidates, no blacklist to maintain.
2. **Soft match accepted a subset of the name's words.** It dropped words ≤3 chars, so
   `job-search` could match on the bare word "search" — which is how a Hermes story landed in
   job-search on the second attempt. It now requires *every* word of the name. A single-word
   project can't reach that branch anyway, since the phrase pass already caught it.

Result: all five known-good titles still resolve confidently
(`job-search/job-radar`, `resume-engine`, `token-usage-in-tray`, `terraria-sequel`,
`assisted-apply`), and the five genuinely ambiguous board rows return **no match** and raise
an Inbox card instead of inventing a folder. A wrong project is worse than no project — it is
the value that would later authorize a write.

### Bug fixed 2026-08-17: `--dry-run` wasn't dry

The dry run ingested all six stories, so the next *real* pulse reported "0 new" and hid them.
The connection runs in autocommit, and only the `pulses` insert was behind the `dry_run`
guard — `sample_usage()` and `sync_notion()` had already written. The pulse is now a single
explicit transaction that `run()` rolls back when dry.

### Bug fixed 2026-08-17: a clean tick "failed" because of its own output

The first scheduled run exited 1 with `UnicodeEncodeError: '→'`. Redirected stdout on
Windows is cp1252, which cannot encode the `→` and `·` the pulse prints. The ledger row had
already been committed — only the printing died — so the loop was healthy and the exit code
said otherwise. `cli.main()` now calls `_force_utf8()` before anything else. Formatting must
never decide whether a run succeeded.

### Bug fixed 2026-08-17: the dashboard's first job was to expose a phantom agent

The Colony panel came up showing `investigator` running for over an hour. It wasn't. The
22:00 pulse had been killed mid-groom by the scheduled task's own `ExecutionTimeLimit` —
**PT10M, shorter than the two 7-minute grooms the same task was authorised to run** — and
because `run_ticket` opens the `runs` row *before* spawning (so cost survives a crash), the
kill left a row saying `running` forever. Two fixes: the limit is now `PT30M`, and every
pulse calls `reap_orphaned_runs()`, closing any run older than 20 minutes as `timeout` with
the verdict `orphaned: parent process died mid-run` and blocking its ticket. Tokens already
spent stay recorded — an orphan is an unknown ending, not a refund. It counts as an anomaly,
not a reason to wake: paying a model to look at a process that no longer exists buys nothing.

A dashboard reporting live work that isn't happening is worse than no dashboard, and this
one found that out about itself within a minute of first rendering.

### Bug fixed 2026-08-17: the windowed launch died with no way to say why

`python -m colony dash` worked; launching it with `pythonw.exe` — no console, which is how a
desktop app is meant to start — exited 1 instantly and silently. Cause: `cli.py` computed
`_COLOR = sys.stdout.isatty()` at **import** time, and under `pythonw` `sys.stdout` is None.
The one stream that would have reported the problem *was* the problem. `_COLOR` is now
None-safe, `_force_utf8()` swaps a missing or unwritable stream for `os.devnull`, and the
window keeps its own `.colony/dash.log` so a silent GUI death is never debugged by guessing.

## Why it matters beyond the tooling

From the Notion idea: it is a portfolio piece (a working multi-role agent ecosystem), it
builds real scrum fluency from the PO seat rather than the team seat, it maps cleanly to IAM
thinking (what permissions each role needs — see `ROSTER.md` §2, which is that argument in
miniature), and every skill the forge promotes makes the next sprint cheaper than the last.
