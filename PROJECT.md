# Colony Dash — Master Memory

**Status:** **M0 → M5 shipped.** The loop is live and grooming on the hour, there is a window
to watch it in, that window can *act* — hire, dispatch, approve a patch, halt everything — and
as of 2026-08-18 the link to Notion runs both ways: the colony reads the checklist it is being
judged on, and the PO can set a status, tick a box or leave a comment from the dashboard.
Priority: **High.**
**Created:** 2026-08-15 (as "PO Dashboard"; renamed **Colony Dash** 2026-08-17)

The orchestrator loop and dashboard where the PO acts as Product Owner over a colony of
Claude agents run by Ordis as Scrum Master. Intended to become the **main dashboard for
The PO's integration with Claude as a whole** — not a side tool.

- `ARCHITECTURE.md` — the full backbone: state model, pulse mechanics, budget, dashboard, forge.
- `ROSTER.md` — where agents come from (agency-agents) and how they get hired.
- `colony/` — the code. `python -m colony dash` opens the window; `status` is the text view.
- This file — status and decisions only.

## Running it

```
python -m colony init                 create + seed the ledger, scan the roster
python -m colony dash                 open the dashboard window (pywebview)
python -m colony dash --serve         serve only, no window — browse 127.0.0.1:8787
python -m colony dash --host auto --serve
                                      reach it from a phone for this session; 'auto' is
                                      the tailnet address, else the private LAN one.
                                      Refuses to start on a non-loopback address without
                                      COLONY_ACCESS_TOKEN
python -m colony autostart            serve from logon so the phone finds it already up
                                      (--show reports it, --remove unregisters it)
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

- the PO = **Product Owner** — owns backlog, priority, approvals.
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
- **Blast radius: all of the projects root, read.** Write is only ever the one project
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
- **agency-agents is a hiring pool, not a fleet.** 255 personas at `~/.agency-agents`.
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
Approved work lands **uncommitted** in the real folder — the PO reads the diff in the drawer
and commits it themselves. Nothing in `control.py` spends tokens: approving marks a story
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
  violet is Ordis, mint is what they learned, and the dim line is the question
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
- [ ] **Approve or reject the first hire Ordis picked themselves.** Inbox card #19: Developer
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
- [ ] **Decide what happens to branch `mobile`.** Local intake, the PWA, the access
      gate and the logon task all live there and are tested but unmerged. Nothing on it changes behaviour on
      a loopback bind: the gate is off unless `--host` is passed, and `＋ story` is an
      addition rather than a change to any existing path. Merge when it has been used from
      an actual phone for a few days.

## Finished 2026-09-02 — the launch that lost the race, and the screenshots that named real work

- [x] **MIT `LICENSE` added**, and a `## License` section at the end of the README. The
      copyright line carries a real name, which is the one place identifying data survives.
      Swapping it for a GitHub handle is a one-word edit; git's own commit history carries
      the name either way, so scrubbing only the LICENSE would not hide anything.
- [x] **`mobile` merged into `master`.** Fast-forward clean, nothing behind. The main branch
      here is named `master`, not `main`.
- [x] **The "how it works" diagram spread out.** Same shape as before; the pulse moved to the
      top rank beside the two intakes, node and rank spacing well past the mermaid defaults,
      and four long labels hand-wrapped so the boxes stop growing into each other.
- [x] **A real GitHub rendering bug caught in the `pulse-log.png` caption.** Markdown backticks
      inside an HTML block render as literal backticks on GitHub, because GitHub does not parse
      markdown inside raw HTML. Now `<code>` tags.
- [x] **Screenshots blurred.** Ten of the nineteen carried real folder names, story titles or
      a whole chat thread. The interface around them is untouched, which is the part a
      screenshot exists to show. Script kept out of the repo; the committed PNGs are the
      blurred ones.
- [x] **`colony-dash-only` branch cut** by `git subtree split`, 67 commits, colony-dash at the
      root, verified free of `.env`, machine paths and any name but the LICENSE line.

### Bug fixed 2026-09-02: a headless launch that lost the port never ended

`launch` asks whether the port is free, then binds a moment later. Two callers aim straight at
that window — the logon task, and the phone switch binding a network address from the process
it was pressed in. One loses, and losing is correct, because there is one dashboard on one
ledger either way.

What was wrong is what the loser did next. It logged `another dashboard bound ... first —
reusing it` and fell into the sleep loop written for a process holding its own daemon server
thread. It held a console and an interpreter for a server it did not own, forever. Nine had
accumulated.

`launch` now tracks whether it actually bound the port. A process that did not, returns. The
two fallbacks out of the window path used to recurse back into `launch` to reach that same
sleep, which worked only by accident — the recursion found the port taken and idled anyway —
so they call `_idle` directly now, and only when they own the port.

Three tests in `tests/test_desktop.py`. They patch both `sys.modules["colony.server"]` and the
attribute on the package, because `from . import server` reads the attribute when there is one:
patching only `sys.modules` passes alone and fails under `unittest discover`, where an earlier
test has already imported the real module — and then the tests bind 8787 for real.

### Still open

The push. There is no `gh` on this machine, no git remote on the repo and no stored credential,
so nothing here can reach GitHub yet. The split branch is cut and waiting.


## Finished 2026-09-01, the phone works off the wifi

The PO asked the obvious question after a week of using phone access: does this
really not work over cellular. It really did not, and the reason is one line in
`net.py` that has been correct since the day it was written.

`net.auto()` prefers a tailnet address and falls back to a private LAN one. On
this machine there was no tailnet, so it bound `192.168.x.x`, which is an address
that stops existing at the front door. Everything else about the feature reported
healthy: the task was installed, the port answered, the firewall rule was there,
the QR code scanned. The panel had no way to say the one thing that was wrong,
because the one thing that was wrong was the network the machine happened to be
on.

So the preference is now a thing you can act on rather than a thing you either
have or do not.

**`colony/tailscale.py`** answers four questions and has a button for two of
them. `find()` looks in both install directories, because a machine upgraded
rather than reinstalled keeps the CLI under `Tailscale IPN` in the 32-bit
Program Files. `state()` runs `tailscale status --json` and deliberately ignores
the exit code: a signed-out daemon exits non-zero and still prints the JSON that
says so, and treating the code as authority would turn the most common state into
"cannot tell". `installer()` looks for the newest `tailscale-setup-*.exe` in
Downloads, by modification time rather than by the version in the name, since
`1.99.0` sorts after `1.102.3` as a string and is the older build. It is there
because the install path people actually take is "download it, get distracted,
come back", and at that point the file they need is one they already have and
cannot find.

**`install()` is not silent, on purpose.** The Windows installer's quiet switch
is not something this file can promise across versions, and an installer that
runs invisibly and fails invisibly is worse than one you have to click. It
launches with its normal window and returns immediately; the panel's job after
that is to say "come back and press refresh", which is honest about what the call
actually proved.

**`login()` hands back the URL rather than waiting for it.** `tailscale up`
prints an auth URL and then blocks until the visit completes, so it is started,
read on a thread with `readline` rather than iteration (Windows buffers the
iteration form until exit, and it does not exit until the URL is visited), and
left running. The URL comes back to the panel, which draws it as a QR code as
well as a link. That was the nice accident: the device that most needs to join
the tailnet is the one holding a camera.

**Both buttons are loopback only**, enforced in `server.act_tailscale` and not
only in the panel that hides them. This starts an executable off the disk with a
UAC prompt behind it, and that is not a thing a request arriving over a network
gets to do however good its token is.

**`net.advice("lan")` now says the part that matters.** It used to describe the
security tradeoff and say nothing about range. It now leads with "this address
only exists inside your building" and names cellular, because that is the
sentence that would have answered the question a week earlier. `advice("tailnet")`
stays empty: every caller prints this behind a `⚠`, and good news behind a warning
sign is how a panel teaches people to stop reading it. The positive case is said
by the Tailscale block instead, where it is not a warning.

Fourteen tests in `tests/test_tailscale.py`, none of which run `tailscale` or
touch the network. Suite is at 200.

The README grew a **Tailscale makes it work off your wifi** section with the
five panel states as a table, and the honesty section's "a tailnet fixes 1 and 3"
line now points at a button rather than at homework.

Also in this pass, from the same review: the `How it works` diagram gained an
optional `Notion board` node beside `＋ Story` at the top, so both intakes are
visible as the choice they are; the whole README was rewritten for the `unslop`
style rather than only spot-edited, mostly splitting sentences that had to be
read twice; and the 2026-08-24 journal entry now names `unslop` rather than the
skill it replaced.

## Finished 2026-09-01, the repo stops naming its owner

The push to GitHub was the forcing function. A sweep of every tracked file for the
PO's name, their Windows username and the absolute path of this machine found
hits in `ARCHITECTURE.md`, `PROJECT.md`, `ROSTER.md`, `pulse.py`, `projects.py`,
`db.py`, `wake.py`, ten migration comments, and four screenshots.

Two of those were more than cosmetic.

`wake.py:703` guarded against double-prefixing a settled line by checking
for a prefix built from the PO's own name, while `wake.py:571` tells the model to begin such a
line with `the PO says` and `wake.py:704` writes `The PO says: `. The guard had
stopped matching the string it guards, so a settled line written the way the
prompt asks for it would come back as `The PO says: the PO says: ...`. Now it
checks `the po says`.

Editing comments in ten already-applied migrations would have bricked every
existing ledger. `db.py`'s `migrate()` hashes each `.sql` file and refuses to run
when an applied file's hash moves, which is the right rule and the reason the
edit was dangerous. `db.py` now carries `SUPERSEDED`, a dict from filename to the
exact earlier digests this file vouches for. A listed digest is forgiven once and
the row is immediately rewritten to the current hash, so the exact check is back
on from the next start and the list cannot rot into a hole. Three tests in
`tests/test_ledger.py` cover the accept-once path, that forgiveness is per file,
and that no entry names a migration that does not exist. Verified against two
fresh copies of the live `.colony/ledger.db`.

Capitalization was scoped by reading `app.css` first. `#d-eyebrow`, `.blk > .lb`
and `.link` are already uppercased by `text-transform`, so only `#d-title`
renders its string literally. Five titles changed: `New Story`, `Add a Persona`,
`Write Approval`, `How the Colony Works`, `Talking Directly, with a Shell`.

`+ Story` is now a mint bubble (`.link.go`), because filing a story is the one
control on the board a new reader is meant to find, and it was the same grey as
`HIDE FILED`.

The four screenshots that show changed text were repainted rather than retaken,
with `scratchpad/shots.py`. The font was identified by measurement: rendering the
old strings in Segoe UI Variable Text at the fitted size reproduces the original
line breaks exactly. `docs/console.png` had this machine's absolute root in the path
chip; `PROJECTS` was moved ten character cells left rather than re-rendered,
because the CSS size is fractional. `docs/story-chat.png` had the PO's first name
in four ledger rows and now says `the PO`, which is what the rest of the app calls
that role.

`README.md` was rewritten end to end: sentence-case headings, no em dashes, all
nineteen images centered with centered captions, and a flowchart that starts at
the `+ Story` button. Notion is gone from the diagram and appears four times in
the prose, all of them saying it is optional. It is not removed entirely because
`colony/notion.py` exists and the layout section would be lying if it omitted it.

Suite at 186.

## Finished 2026-09-01 — the README becomes the manual

Nineteen screenshots arrived, and with them a question worth answering once: does the
README double as the manual, or is a separate manual the right shape. One file, and the
README is it. A repository's README is the document that actually gets read; a MANUAL.md
beside it is read by nobody and drifts within a month, at which point there are two
documents and no way to tell which one is lying. The split that already exists is the
correct one — README is what the thing is and how to run it, ARCHITECTURE.md is why it
is built that way, ROSTER.md is the hiring model, PROJECT.md is this log.

So the README grew a **The dashboard, panel by panel** section between "How it works" and
"Where the agents come from", carrying the board, completed, story replies, files, spend,
macros, the forge and the themes; the shots that support an existing argument went inline
in the section that already makes it — the pulse log under "the pulse writes a row even
when nothing happened", the console under the asymmetric switch, the phone panel under
the setup it describes. The two full-page scrolls sit in a `<details>` block so the page
is not eleven screens of screenshot before the safety model. A contents line at the top
links the eight sections a reader actually goes looking for.

**Two screenshots were edited before being committed, and it is worth being exact about
why.** `PHONE ACCESS.png` showed a live access token three times over — in the pairing
URL, and encoded in the QR code beside it, and the LAN address above both. That token had
already been rotated, but a token in a public repository's README is a token in a public
repository's README, and the QR code is the part that would have been missed by anyone
scanning the text. All three are boxed out. `ADD PERSONA.png` carried a Windows username
in the home directory it names; the path now reads `C:\Users\<you>\.colony-agents`,
which is also the more useful thing for a reader to see. The originals stay on the
Desktop; only the edited copies are in `docs/`.

Nothing executable changed, so the suite is still 177.

## Finished 2026-09-01 — the boundary gets a switch, and the switch only turns one way

Follow-on from the section below, from one question: can opening the console to the network
be a button instead of a file edit, and does putting it on a phone break the point of it.

The answer to the second is yes, and it is the whole design. A switch that disables a
security check is worth nothing if whoever the check is keeping out can also flip it. But
that is a fact about *who may press it*, not about whether it should exist — which is where
the original reasoning ("an env var and a restart, not a button, because a switch that turns
off a security boundary should not be findable by accident") was half right and wrong in the
half that mattered. Being hard to find is not a security property.

**So the switch exists, and it is asymmetric.** `act_console_remote` applies the same
peer-address check to turning the boundary *off* that `_desk_only` applies to the console
itself; turning it back *on* works from anywhere. The general rule, worth keeping if another
switch of this shape ever appears: **you may tighten from anywhere and loosen only from the
desk.** Closing it from a phone is not only safe, it is wanted exactly when you are away from
the desk and have realised the phone in your pocket can open a shell at home.

It writes `COLONY_CONSOLE_REMOTE` into `.env` so the choice survives a restart, and sets the
live value so it does not need one. Every move of the boundary now writes a ledger note,
which is a better answer than the restart it replaced: there is a timestamp for when the
console became network-reachable, which the file-edit version never had.

**`db.set_env_value` is new, and is the same rewrite `phone.rotate` already had.** Only lines
starting `KEY=` change, everything else goes back byte for byte with its comments and line
endings, and the result moves into place with `os.replace`. That file holds the Notion token
and is the only copy of it, so having two slightly different rewrites of it was a bug waiting
for its turn. `rotate` now calls it and its eighteen tests still pass unchanged, which is the
evidence that the factoring did not move any behaviour.

**A real bug fell out of this one too.** `paint()` in the console panel set `box.disabled =
false` on every poll, so the read-only state the previous session added unlocked itself about
a second after it was drawn. The server refused the send either way, so nothing was ever
exposed — but the page said "type here" and then threw a 403, which is the worst of both.
`paint` now respects the boundary, and the explanation of it sits above the box permanently
rather than being a note that appears once.

Tests: 177 passing, up from 171. Six new in `test_safety.py` (`TheSwitchIsAsymmetric`) —
loosening refused from the LAN and from an unknown peer, tightening allowed from the LAN,
the Notion token surviving three flips, and a failed `.env` write leaving the process and the
file still agreeing about what is allowed. See ARCHITECTURE.md §10.25.

## Finished 2026-09-01 — the roster stops belonging to the repo, and the shell gets an address

Two things, and they turn out to be the same question asked from opposite ends: what is this
program allowed to own, and what is a copyable secret allowed to reach.

**Personas are no longer part of this project.** The roster used to be scanned from exactly
one folder — a clone of `msitarzewski/agency-agents` at `~/.agency-agents` — which made
somebody else's git repository a hard dependency of ours. A stranger cloning Colony Dash got
`FileNotFoundError` and an instruction to install a library from a project that is not this
one; and a persona written by hand had nowhere to live, because the next `git pull` in that
clone would clobber it. The scan now reads two roots: `~/.agency-agents` (read only, never
written by anything here) and `~/.colony-agents` (written by the dashboard, unknown to
upstream). Both are optional, neither is committed, and a local persona shadows an agency one
with the same `division/filename` — which is the whole override mechanism.

**Standby → `＋ persona`** is the panel that makes that usable: drop a `.md` and its
frontmatter fills the form, or type it. The division is a combo box over the departments that
already exist, and typing a new one creates the folder — categorisation stays the user's
choice rather than being inherited from whatever folders a stranger happened to ship. The
frontmatter is rebuilt from the fields, so an imported file carrying `tools:` or `model:`
loses it on the way in. `rescan` beside it re-reads both folders. A persona this machine
wrote gets a delete button; an agency one does not, and the server refuses it too.

**The console now only takes commands from the machine it runs on.** This was the largest
real hole before pushing to GitHub, and it was hiding in plain sight: every route here is a
window onto a ledger, where a stolen access token is worth reading the board — except the
console, which is a shell by design and would make the same token worth arbitrary code
execution on the machine holding `.env`. Two very different blast radii behind one lock, and
that lock crosses a home LAN over plain HTTP. So the four console *write* routes are scoped
by peer address (`_desk_only`), the way token rotation already was. Reading the transcript is
unchanged and works from anywhere. `COLONY_CONSOLE_REMOTE=1` in `.env` lifts it for anyone
who wants the console on their phone — an env var and a restart, not a button, because a
switch that turns off a security boundary should not be findable by accident. *(Superseded
the same day: it is a button now, and asymmetric. See the section above.)*

A real bug fell out of writing the test for that: `access.is_loopback("")` is True by design
(for the token gate, an unknown peer must fall back to *asking for a token*), so a request
with no peer on the scope would have passed the console guard. `_desk_only` now tests that
case itself instead of borrowing the answer.

Also scrubbed a home-directory path out of `ROSTER.md` and `PROJECT.md`, the last personal
paths in any tracked file, and documented `COLONY_CONSOLE_REMOTE` and both persona folders in
`.env.example`.

Tests: 171 passing, up from 147. `tests/test_roster.py` is new — two roots, shadowing, path
traversal, the write round-trip, delete refusing an agency persona — plus five in
`test_safety.py` for the console boundary, including one that fails if a fifth console route
is ever added without the guard. See ARCHITECTURE.md §10.24 and §10.25.

## Finished 2026-09-01 — the token gets a button, and the log stops crying wolf

Follow-on from the section below, and both halves came out of reading
`.colony/dash.log` after the phone finally worked.

**Rotating the access token is a button now.** It sits beside the QR code in
`file > phone`, and `py -m colony phone --rotate` is the same thing from a
terminal. Before this, changing a token meant minting one, opening `.env` in an
editor, replacing a line, saving, and re-running a command to get a matching QR
code — five steps to undo one mistake, and the mistake is the kind you want
undone immediately. `phone.rotate()` rewrites only the `COLONY_ACCESS_TOKEN=`
line, writes through a temporary file and `os.replace` so a crash cannot take the
Notion token with it, and is refused from anywhere but loopback: rotating from a
phone would log that phone out in the middle of its own request. The panel also
grew a plain **refresh**, because nothing in it is on the live feed by design and
the address, the firewall and `serving` all change underneath it.

**The bind race stopped writing tracebacks.** `_reusable` asks whether an address
is free and the bind happens a moment later; the logon task and the phone switch
both land inside that window on purpose, and one of them has to lose. Losing is
correct — one dashboard, one ledger — but uvicorn answers a failed bind with
`sys.exit(3)`, which reached a thread exception handler and put forty lines of
asyncio internals into the log several times a session. `SystemExit` is now
caught in both serving threads and the outcome is decided by asking whether the
port answers, which is the only question that separates *nothing is listening*
from *something else is listening*.

Tests: 147 passing, up from 140. The seven new ones are all `rotate()` against
`.env`, and they are really one test asked seven ways: the Notion token on the
line above is still there afterwards, byte for byte, with its own line endings.

See ARCHITECTURE.md §10.23.

## Finished 2026-09-01 — the phone loads forever, twice

The switch from the previous section worked and the phone still did not. Two
faults, stacked, neither of which produced an error anywhere: a browser spinner
at one end and nothing in any log at the other.

**Nothing was listening on the address in the QR code.** `desktop.launch` reads a
marker file to avoid starting a second server on a port that already has one. It
believed the marker in both directions, and only one is sound — the logon task
asked for the LAN address, found the desktop dashboard on `127.0.0.1`, decided
that counted, and exited. The decision is now `desktop._reusable`: the marker is
believed only when loopback is what was asked for, because a loopback server does
not satisfy a request for a network address.

Pressing the button is a separate problem from the next logon, so
`server.serve_extra` binds the network address in the running process rather than
waiting for a second one to win a race it should lose. One app, two sockets. That
needed `TRUST_LOOPBACK`, a flag set only by `serve_extra`, so arming the token
gate does not log out the desktop page the switch was pressed in.

**Windows Firewall had no rule for the port.** A dropped packet is not a refused
one — a refusal reaches the browser in milliseconds, a drop looks like a server
still thinking. The trap is that Python has two executables: a terminal run is
`python.exe` and gets the "allow this app" prompt, while the logon task and the
shortcut are `pythonw.exe`, a different file and therefore a different rule, and
a hidden background task has no window to prompt in front of. So it works when
tested from a terminal and fails on the machine you walk away from.

`firewall.py` reads the rule state unelevated, reports `open` / `blocked` /
`unknown` in the panel and the CLI, and `py -m colony phone --allow-firewall`
writes it behind one UAC prompt. The rule is one port, TCP, inbound, private
profiles — not the program, which would open every port any Python script here
ever binds.

Tests: 140 passing, up from 127. New: `tests/test_desktop.py` and
`tests/test_firewall.py`, plus three gate tests for the loopback exemption.

## Finished 2026-08-30 — phone access becomes one button

Still on branch `mobile`. The previous section left phone access working and, for a
different reason, still not something you would set up: it was five steps, and one of them
was editing a credential file by hand. Mint a token, open `.env`, paste it, save, run
`py -m colony autostart`. All five happen at the desk, on the machine you are about to walk
away from — which is the argument for doing them from the page already in front of you.

**The dashboard grows a panel.** `file → phone` opens a drawer with a switch, the address,
and a QR code. `POST /api/act/phone` resolves an address, mints a token if there is not
one, registers the logon task and starts it. `GET /api/phone` answers the panel. Neither is
part of `/api/state`, because answering costs a PowerShell call and a socket probe, and the
live feed polls every few seconds for a value nobody is reading.

**`py -m colony phone`** is the same thing from a terminal — state by default, `--on` to
set it up, `--off` to stop serving at logon. `autostart` stays, because it is the command
that shows what the scheduled task actually holds when something is wrong.

Two rules constrain the write, and both exist because a web request is now editing `.env`.

**A token is written only when there is not one.** A request that can rewrite the access
token is a request that can lock a paired phone out of the ledger by accident, and the
accident looks exactly like the button working. An existing token is used as it is.

**The write is an append, not a rewrite.** No parse, no round trip through a dict, no
reformat. `.env` holds the Notion token beside the access token, and a file that is only
ever appended to cannot lose the line above it. The conditional leading newline goes both
ways: a file already ending in one must not grow a blank line per call, and a file not
ending in one must not get the token glued onto the end of the last value. Both are tests.

Turning it off removes the task and stops there. The token stays, because deleting it would
log out a phone that is paired and working; "off" means the server stops coming up on the
network, not "forget everything".

### There is a QR encoder in the repo now

The address is a private IP, a port and a 43-character token, which is not a string anyone
should retype on a phone keyboard. Every library that draws a QR code is a fine library —
this one is hand-written because the install story for this project is four packages, and
"it also needs a QR encoder" is a worse trade than three hundred lines that never change
again. The format was frozen in 2000.

`colony/qr.py` is deliberately narrow: byte mode, versions 1 through 10, no ECI, no
structured append, no kanji. That covers 271 bytes and refuses rather than guessing beyond
it. The real payload is about seventy and lands on a version 5 symbol at level M.

It was written against `segno` as an oracle, and then the oracle was thrown away. 960
pinned comparisons — every version, every error level, all eight masks, three lengths each
— plus 200 fully automatic ones, a 3,908-symbol ASCII sweep, and both sides of every
capacity boundary, all module-for-module identical. What survives in `tests/test_qr.py` is
a set of matrix hashes plus a decoder that reads the symbols back out: format information,
unmask, de-interleave, recompute every block's Reed-Solomon codewords, recover the string.
The fixtures prove the output has not changed; the decoder proves it was right to begin
with, and it is the half that survives someone regenerating the fixtures.

Five bugs of ours turned up in that comparison and one in `segno`. The one worth writing
down is ours. **The mask is scored before format and version information are written.**
ISO/IEC 18004:2015 §7.8 is explicit about the order, `segno`'s source carries the comment
"DO NOT add format / version info in advance of evaluation", and getting it wrong is
invisible: every mask still produces a valid symbol, the penalty scores are merely all
shifted by roughly the same constant, so only a close comparison between two candidates
tips the wrong way. It reads as a filing detail and is a correctness one. Same story for
the dark module at `(8, size-8)` — it belongs to the format block rather than the skeleton,
because leaving it set during scoring puts one stray module into all eight comparisons.

`segno`'s own bug, for anyone repeating this: `write_padding_bits` does
`[0] * (8 - length % 8)`, which appends a whole zero byte when the stream is already
byte-aligned — which is every byte-mode symbol below version 10. The reference
implementation is `(8 - size % 8) % 8`. The oracle was patched before the comparison ran.

The terminal draws the same code in half-block characters, two module rows per line,
because a character cell is twice as tall as it is wide and one module per cell comes out
stretched and, on a narrow window, wrapped and unscannable. Dark modules print as the
*light* half-blocks — a terminal is light-on-dark, and the naive mapping is a photographic
negative that will not scan. And the encoding of the output stream is checked before
anything is drawn: `_force_utf8` reconfigures with `errors="replace"`, which is right for
every other command in this CLI and wrong here, because a QR code with question marks in it
is not a degraded QR code, it is a rectangle that looks like the feature working.

Tests: 127 passing, up from 106. `tests/test_qr.py` is 20 of the new ones and
`tests/test_phone.py` is 11.

## Finished 2026-08-29 — the server is up before you are

Still on branch `mobile`. Reaching the dashboard from a phone worked from the moment the
server could bind a network address, and then did not work in practice, because it only
ran while a terminal was open on the desktop. The phone is the device you use *because*
you are not at the desk.

**`py -m colony autostart`** registers a hidden logon task, the same shape as the hourly
pulse and beside it: `pythonw` so there is no console, hidden, `--log` to `.colony/dash.log`.
`--show` reports it, `--remove` unregisters it, and installing also starts it, so the
command is not a thing you run and then have to do something else about.

Three differences from the pulse task, each of which is a bug if you get it wrong.

**No execution time limit.** Task Scheduler's default is three days and then it kills the
task. A server that stops on the third Tuesday and returns at the next logon is worse than
one that never started, because the first you hear of it is a phone that cannot connect.
`--show` prints whether the installed limit is `PT0S` or the dangerous default.

**`--host auto`, resolved at every launch rather than written into the task once.** An
address is a fact about the network at boot; a task holding a literal `100.x.y.z` fails
silently on the first day that address changes, and it fails looking like "the phone
stopped working". The new `colony/net.py` prefers a tailnet address (`100.64.0.0/10`),
falls back to a private LAN address and says so differently, and *raises* rather than
binding anything else.

That last part found a real bug in its own test: the obvious way to ask "is this address
private" is `ipaddress.ip_address(x).is_private`, and in Python 3.12 that answers True for
the documentation and benchmarking ranges — `203.0.113.7` and `198.18.0.1` both pass. An
address being reserved is not the same as it being your house. The three RFC 1918 networks
are now spelled out explicitly.

**Restart on failure and a 45-second start delay**, both covering the same thing: losing
the race with the network at logon, which is the one failure that is actually likely.

`preflight()` asks `access.check` the same question the server will ask before registering
anything, so a missing `COLONY_ACCESS_TOKEN` is a refusal in the terminal rather than an
exit code in a log at seven in the morning.

### The duplicate-server bug this created

`launch()` decided whether a dashboard was already up by probing one address, which was
correct for as long as the only address was loopback. A logon task binds the network
address instead, so `_port_is_free("127.0.0.1", 8787)` answers True while a dashboard is
running — and double-clicking the Desktop shortcut raises a second server on the same port
on a different interface. Two dashboards, one ledger, no error anywhere.

Fixed with a marker: a running server records the address it bound to `.colony/dash.url`,
and `launch()` reads it. The file is a hint and never a fact — it outlives the process that
wrote it every time — so the port behind it is probed before it is believed, a stale
marker is discarded, and a marker for a different port is ignored rather than trusted.
Verified live: a server bound to the LAN address, then a default loopback `launch()`, which
logged `already serving on http://10.0.0.57:8819 — reusing it` instead of binding.

Suite is 72 → 96 tests, 2.3s, in the new `tests/test_autostart.py`. Nothing in it talks to
Task Scheduler — registering a real task is a change to the machine running the tests, and
the parts worth asserting are the two decisions made before that point. The task itself was
installed, inspected and removed once by hand.

Written up as ARCHITECTURE.md §10.20.

## Finished 2026-08-27 — the phone, and the door beside Notion

Built on branch `mobile`, not on `master`, because the ask was explicitly a trial.
Three things that sound like one feature and share almost nothing: file a story without
Notion, read the board on a phone, reach the server from off the machine.

**Notion is not retired and was not touched.** It is now one of two intake doors rather
than the intake. The `＋ story` button on the board panel writes straight into `stories`
with a NULL `notion_page_id`.

**The schema already allowed it.** `notion_page_id` has been nullable since migration 001,
with the column comment "NULL for loop-authored stories". The only `INSERT INTO stories`
anywhere is the Notion path, and the sync loop iterates what Notion returns rather than
reconciling against the table — there is no reaper. A locally-filed story is invisible to
intake, not at risk from it. The only schema change needed was migration 026, widening the
`po_actions.action` CHECK to admit `'story'`, which SQLite cannot do in place, so it is
the same full-table rebuild as 006, 008, 018, 020, 021 and 023. Verified against a copy of
the live ledger — 140 real `po_actions` rows and their foreign keys — rather than a fresh
one, because a fresh database has neither.

**A named folder on a filed story is `confirmed`, not `inferred`.** Same distinction §8.2
draws for `confirm_project`. But `create_story` refuses a folder that does not exist, where
`confirm_project` does not: in the Inbox a typo costs one more question, here it would
become the confirmed write scope at the moment of creation with nothing left to catch it.
Filed with no folder, a story raises exactly one `needs-info` escalation.

**Installable, not a second app.** Manifest, three generated icons, and a service worker
that caches nothing but an offline notice — a cached `/api/state` is yesterday's board with
today's confidence, and a cached `app.js` is the exact bug the `no-store` headers exist to
prevent. The worker is served from `/sw.js` at the root because a worker only controls
pages at or below its own path.

**Responsive was mostly undoing scrolling.** `.tiles`, `.done-list` and `.flight .rail`
scroll internally, which is right at 1500px and a trap on a phone, where a swipe that
starts inside a box moves the box and the page looks frozen. Under 720px they let the page
scroll. Also `100dvh` over `100vh`, a hard 16px floor on input type so iOS does not zoom in
and refuse to zoom back out, and `viewport-fit=cover` with `env(safe-area-inset-*)`.

**The `X-Colony` header was never an access control.** It is CSRF protection; the access
control was the loopback bind. So `access.py` arms off the bind rather than off a setting:
`check(host)` returns False on loopback, True with a token configured, and *raises*
otherwise — there is no path through `serve()` that reaches `uvicorn.run` open and
tokenless, and that is the property `tests/test_access.py` is really about. The gate
answers a navigation with the login page and everything else with a 401, discriminating on
`Accept` rather than a list of paths that would need maintaining.

Cookie is `HttpOnly`, `SameSite=Lax`, 90 days, and deliberately not `Secure` — a tailnet
address is plain http and a cookie the browser will not store is a login loop. `?k=<token>`
makes the first visit a link or a QR code, then the middleware swaps it for the cookie and
the page strips it from the address bar.

Suite is 41 → 72 tests, 2.6s. New: `tests/test_intake.py` (11) and `tests/test_access.py`
(21, including the middleware driven by hand — `starlette.testclient` wants httpx on this
machine and does not get it). Smoke-tested live: every route including the manifest, the
worker, the icons and the favicon; a story POSTed through `/api/act/story` against a copy
of the ledger; and the gate proved to hand a 401 to `/app.js` and the login page to `/`.

Written up as ARCHITECTURE.md §10.18.

### What did not get built, and why

**Accounts.** Asked for as "the same work on desktop and phone", which is worth separating
from what accounts actually are.

Desktop/phone parity came free with the network bind. One server, one ledger, one
filesystem — the phone is a second view of the same state, so there is no sync step and
nothing to reconcile. Accounts would introduce the problem they are usually brought in to
solve.

Per-user API keys solve the cheap half of hosting. The expensive half is that this program
spawns the `claude` CLI against real files in real git worktrees on a real disk, so hosting
a second person means hosting their filesystem, their git remotes, their `claude`
authentication and their worktrees — with one tenant's build agent one path-traversal bug
away from another tenant's repo. The §8.2 write-scope rules are written against one
operator's directory tree; they are not a sandbox, and treating them as one because a login
now exists would be the worst available reading of them.

The ledger design would survive that port. The execution model would not. So the boundary
is: this program is single-operator by construction, `access.py` is remote access rather
than authentication, and the multi-tenant version starts from this database design and none
of this execution model. ARCHITECTURE.md §10.19.

## Finished 2026-08-27 — the repo becomes something a stranger can clone

Groundwork for putting this on GitHub as a portfolio piece. The audit that preceded it
found the code in better shape than the repository: zero `TODO`/`FIXME`/`HACK` across
11,898 lines of Python, `.env` never committed at any point in the 53-commit history, no
token-shaped string in any tracked file, and a dependency surface of four packages. What
was missing was everything that turns a directory of source into a project someone else can
run.

### The hardcoded root

`db.py:25` read `PROJECTS_ROOT = Path(...)` with one machine's absolute path in it. Every other module derives
its root from that one line — `control.ROOT_POSIX`, `projects.ROOT`, `runner.ROOT`,
`worktree.ROOT`, `pulse.PROJECTS_ROOT`, `forge.SKILLS_DIR` — so it was a single point of
change, and a single point of failure for anyone else.

It now resolves to `PROJECT_DIR.parent`, the folder containing the checkout, overridable
with `COLONY_PROJECTS_ROOT` from the environment or `.env`. On this machine the derived
value is byte-identical to the literal it replaced, verified before the edit landed, so the
running colony sees no change at all.

The `.env` read is a new `db._env_value`, not the existing `mirror.load_env`. That
distinction is load-bearing and is recorded in ARCHITECTURE §8.1: `load_env` pulls the
entire file into `os.environ`, and `db` is imported by `console.py`, which hands its
environment to a `claude` subprocess. Using it here would have placed `NOTION_TOKEN` in
front of the one agent explicitly forbidden from reading `.env` — a security regression
disguised as code reuse. `_env_value` reads one key and mutates nothing.

### The files that were missing

- **`README.md`** — the pitch, a mermaid flow of the pulse, the ledger rationale, and the
  safety model at length. The section on `--dangerously-skip-permissions` in `console.py`
  is deliberate: unexplained, that flag reads to a reviewer as a footgun; explained as the
  human-only second door with no import path from `pulse.py` or `wake.py`, it reads as the
  design decision it is. The distinction is worth a reader's first two minutes.
- **`requirements.txt`** — fastapi, uvicorn, pywebview, pillow pinned to the versions this
  has run against; both MySQL drivers commented out, since nothing imports one unless
  `colony mirror` is called.
- **`.env.example`** — every key optional, with the Notion "add the integration under
  Connections" step spelled out because forgetting it is the usual cause of an empty intake
  with a valid token. The `COLONY_*` tuning knobs are listed as *shell-only* and marked as
  such: `build.py` and `wake.py` read them at import time, before any `.env` is loaded, so
  putting them in that file would silently do nothing.

### Verified

Every module imports. `py -m colony status` runs against the live ledger unchanged.
`COLONY_PROJECTS_ROOT` propagates to all six derived roots. And the real test: a copy of
`git archive HEAD` with no `.env` at all, in a temp directory, ran `py -m colony init`
clean — 25 migrations applied, two structural agents seeded, sprint opened, 270 personas
scanned, and a projects root derived from its own location.

### Three more copies of the same literal

The `db.py` fix above felt like the whole job, because every module derives its root from
that one line. A grep for the *value* rather than the constant found it spelled out again
in three more places: `seed.READ_SCOPE`, `control.DEFAULT_READ_SCOPE`, and the console's
system prompt. All three now derive from `db.PROJECTS_ROOT`, and all three produce a
byte-identical string on this machine.

These were the more dangerous of the four. A wrong `PROJECTS_ROOT` fails loudly; a wrong
*read scope* does not fail at all. A stranger running `colony init` would have seeded and
hired agents pointed at a drive letter that does not exist on their machine, and every one
of them would have come back having searched and found nothing — correctly, quietly,
forever.

### A test suite, in the standard library

41 tests under `tests/`, run with `py -m unittest discover -s tests`. No pytest: the
dependency surface here is four packages, and a suite that needs an install before it runs
is a suite nobody runs on a fresh clone.

What they cover is deliberately not "the code" — it is the set of claims ARCHITECTURE.md
makes that are otherwise only promises. Migrations apply in filename order, are idempotent,
and refuse to run when an already-applied file has been edited. `connect` really sets WAL
and foreign keys, and a read-only handle really refuses a write. `ALWAYS_DENIED` beats a
contract that asks for `Bash`, and the write tools unlock only with `allow_writes`. The
write scope refuses `..`, dot folders, absolute paths, and folders that do not exist. The
console admits exactly one turn, and a pending row left behind by a crashed process still
blocks the next send. And `db._env_value` adds nothing to `os.environ` — the leak that
function exists to avoid, asserted rather than reasoned about.

Nothing in the suite spawns `claude` or touches the real ledger. `agent.invoke` is checked
by capturing the argv it would have run; the console lock is checked with `_answer`
replaced by a stub that blocks until the test releases it.

### The dashboard becomes three files

`ui/index.html` was 6,957 lines: 1,830 of CSS, 4,880 of JavaScript, and 250 of actual
markup between them. It is now `index.html`, `app.css` and `app.js` in the same folder,
served by two new routes with `Cache-Control: no-store` — which is what the page already
had by being re-read from disk on every request, and the right answer for a dashboard being
edited while it is open.

Still no build step, on purpose. This page is served from `127.0.0.1` to one person; a
toolchain would be more moving parts than the thing it builds.

Verified by reassembling the three files and diffing the result against the committed
original: byte-identical. That is a better check than reading the diff, because the risk
was never "did the code move" but "did it move exactly".

### Still open before pushing

The push itself. `git subtree split -P colony-dash` to carry the history into a standalone
repo — how the thing was built is evidence, and worth more than a fresh `init`. Screenshots
need dropping into `docs/` and the block near the top of the README uncommenting; a
dashboard project with no picture of the dashboard undersells itself.

## Finished 2026-08-26 — the console gets its controls

Four additions to the console built earlier the same day, all of them things a
terminal has and the first version did not.

**A model dropdown and an effort dropdown**, stored per conversation in
`console_state` (migration 025) rather than hard-coded. Both lists are served from
`console.py` and rendered from that response, so a menu cannot drift from what the
CLI accepts. Changing either mid-flight is allowed on purpose: the turn already
running keeps the pair it was launched with, and the change means "the next one,
please", which is what a person sitting at the page would mean.

**A compact button**, which sends `/compact` as an ordinary turn. Verified that
slash commands do work through `claude -p --resume`; `/compact`, `/context`,
`/cost` and installed skills all answer, while interactive-only ones such as
`/status` reply "isn't available in this environment". Making it a normal message
rather than a special route means it queues behind a running turn, is refused by
the same lock, and lands in the tape with its cost attached.

**A command dropdown** that pastes at the cursor and never sends. Its contents are
read off disk: the three verified built-ins, the user's and the project's
`.claude/skills` and `.claude/commands`, and the install paths named in
`installed_plugins.json`. The first version globbed
`~/.claude/plugins/marketplaces` instead and offered thirty commands of which one
was installed — a dropdown listing commands that do not exist is worse than none.
Cached for two minutes, because the drawer polls this endpoint every 1.5 seconds.

### On the 35k tokens a turn costs

Measured rather than guessed, and the obvious suspect is innocent. `CLAUDE.md` is
2,272 bytes, roughly 570 tokens — under two percent of it. The rest is Claude
Code's own system prompt and its built-in tool schemas, which is the floor for any
`claude -p` invocation.

It should not be trimmed. That prefix is cached across sessions, so a turn reads
it for about $0.007. Cutting the tool set with `--tools Bash,Edit,Read,...` writes
a *new* cache prefix: same 5-token answer, $0.2170 instead of $0.0069.
`--strict-mcp-config` saves 480 tokens, which is noise. The cheap turn is the one
that reuses the prefix everything else already uses.

Also confirmed while checking: clearing the chat really does forget. A fresh
`--session-id` asked for a codeword given to the previous session answered "I DO
NOT KNOW."

## Finished 2026-08-26 — a terminal inside the program

Colony Dash could build anything except itself. Every fix to the dashboard — and
the last several sessions were nothing but fixes — was made from a separate
Claude Code terminal, because the colony's own agents are denied `Bash` by
`agent.py` and write only into a worktree behind a PO approval. That denial is
correct for a loop that fires unattended at 3am. It is the wrong answer when the
PO is sitting in front of the page and wants the page changed.

**The console is a second door, described in ARCHITECTURE.md §8.3.** It is in the
Ordis panel because it is Ordis themselves rather than another department, and it is
the only control on the dashboard that does not wait for a pulse. Full tool access,
`--dangerously-skip-permissions`, `cwd` at the projects root, no ticket and no
worktree between it and the tree.

The guards that remain are the ones that were never about an agent's permissions:
the server is bound to `127.0.0.1`, `/api/console/*` requires the `X-Colony`
header, and nothing scheduled may import the module — `pulse.py` and `wake.py`
have no path into it, which is what keeps §8.1 true for the autonomous half of the
system.

### A conversation, not a series of prompts

The first message of a chat claims a UUID with `--session-id`; every message after
it passes `--resume`. That is the difference between talking to something and
sending it unrelated postcards, and it is why the chat can be cleared at all:
*clear* drops the UUID and bumps `console_state.epoch`, so the next message starts
a session that has never heard of the last one.

Clearing does not delete anything. The turns stay in `console_turns` under their
old epoch, tokens and cost attached. A chat that can erase its own bill is a chat
that can lie about what it cost, and this one already runs without a budget gate.

### Why it polls instead of joining the snapshot

A shell command can take twenty minutes, so `send` records the message, opens a
`pending` row, starts a thread and returns. The page polls that row every 1.5s
while the drawer is open. It is deliberately not on the SSE snapshot: the snapshot
fans out to every panel on the page, and a chat that is only open sometimes should
not be repainting the board while it waits for `npm install`.

One turn at a time, held by a process lock *and* a `pending` row — the lock
catches two requests in the same millisecond, the row catches a stale turn left by
a crash. Two shells writing one tree is the failure the rest of the architecture
exists to prevent, and it is the only one the console could still cause by itself.

Verified end to end against the live CLI: a first turn answered, a `--resume` turn
remembered it, and `git rev-parse --short HEAD` came back `9fa2b72` from inside the
chat — which is the whole point, since no colony agent can run that at all.

## Finished 2026-08-26 — the build that handed work back into silence

"15 Part Job Search" was dispatched twice within three minutes, tickets #80 and
#82, and both came back blocked with the same finding: every buildable criterion
was already shipped, and the one remaining criterion needs a command the agent
has no shell to run. Both agents wrote that command into `needs_run`, which is
the mechanism built for exactly this handover. Neither request reached the PO.

**`_raise_run_requests` was only called on the path that produced a patch.**
`run_one` returns early when the diff is empty, and that early return skipped
the handover, put the story back in `ready`, and left the ticket in the Ticket
Queue reading `BLOCKED IMPLEMENT weekly-funnel-report-builder` — a state, an
intent and a name, and not one word of why. `ready` then means "dispatch me",
so the Inbox invited another dispatch, which produced another empty build and
another silent handover. That is the loop, and it is visible in the ledger as
two identical blocked tickets sitting next to a story the board called READY.

The empty-build path now does what the patch path does. It stores the answer
JSON as the ticket's findings so the skipped criteria and the requested commands
survive on the record, it raises the run-request cards, it records the agent's
`learned` note, and it hands the story to `_park_no_change`.

**`_park_no_change` sends the story to `needs-info`, never back to `ready`.**
Dispatching the same story to the same agent over the same tree produces the
same empty build; `ready` is an invitation to do exactly that. `needs-info`
carries the reason in `blocked_reason`, which the story panel and the Inbox both
read, and it is the lane `pulse.ensure_blocked_visible` guarantees an open card
for on every tick. When commands were handed over, those run-request cards are
the open cards and no second card is raised. When nothing was handed over, a
`needs-info` card goes up listing what the build skipped and why.

### Ignored files the code actually reads

Ticket #80's agent reported that `data/notion_sync_state.json` "isn't in this
worktree", and it was right. `worktree.seed` copied tracked changes and
untracked files, and `git ls-files --others --exclude-standard` excludes
anything gitignored — which that file is, deliberately, because it is a
rebuildable cache. It is also the file that decides what `apply.main auto` will
sync, so an agent that cannot see it cannot say anything true about the sync.

`worktree._ignored_in` adds a third pass. Ignored files inside the scope are
grouped by their first path segment and a group is copied only if it holds
`MAX_IGNORED_PER_GROUP` (12) files or fewer. That is the line between state and
output, and it is drawn by count rather than by name so it needs no list to
maintain: `data/` holds two files and comes in, `packets/` holds 1,582 and does
not. Skipped groups are named in the work order rather than passed over in
silence. Credential files are excluded here at every size — they keep their own
gate in `sees_secrets`, and a second door into the same room would make the
first one a decoration. Nothing from this pass can reach a patch: `git add -A`
honours .gitignore, so none of it enters the base tree.

Measured on the live tree: `job-search` yields three files copied and three
directories named as skipped; `personal-desktop-projects` yields two files;
`colony-dash` yields none.

### Saying why on the page

Three places said a thing was wrong without saying what.

A blocked ticket sat in the Ticket Queue indefinitely. One that has closed is
finished and now appears in Completed instead, where the tile reads `no changes`
rather than `delivered` — calling an empty build a delivery is what let the same
empty build be ordered twice. One still open stays in the queue and now shows
the agent's own summary underneath it.

A story's `blocked_reason` was the last row of the facts list in the story
drawer, below the Notion sync timestamp. It is a banner at the top of the drawer
now, before the facts, because it is the answer to the question that made the
drawer worth opening.

## Finished 2026-08-26 — a failed run that left the story sitting in DELIVERED

Three faults on one card, found together.

**The command never ran.** The build agent wrote
`cd "job-search/assisted-apply"; py -m apply.main auto`, which is how anyone
would type it in a terminal. `runner.execute` already puts the shell in the
project folder, so the `cd` resolved to
`job-search/assisted-apply/job-search/assisted-apply`, and the run died with
"The system cannot find the path specified" before reaching `apply.main`. The
exit code was 1 and the failure was entirely the colony's.

A leading `cd` into the folder the run is already scoped to is now dropped, in
`runner._drop_leading_cd`. A leading `cd` anywhere else is refused, because the
folder a command runs in is the write scope the PO approved. The cleaning
happens when the card is raised, not when it is run, so the card shows the line
that will actually be executed, and the story records that line too.

**The story stayed in DELIVERED while the card said otherwise.** `_settle_run`
sent the story back to `ready` with `AND status NOT IN ('archived','accepted')`.
Story #1 was `accepted`, so the update matched nothing, and the reply on the
card still read "the story is back in the queue". A story whose verification
failed is not delivered. A run that is not `clean` now parks the story in
`needs-info` from any lane but `archived`, with the verdict written into
`blocked_reason`.

**Nothing escalated.** The failed run produced a `blocked` event and no card, so
the board went quiet with a broken story showing as delivered. `_settle_run`
now raises the `needs-info` card in the same transaction. That kind is
deliberate: `pulse.ensure_blocked_visible` guarantees a card for any blocked
story that has lost one, every tick, so a failure here cannot go silent again.

`runner.judge` also learned to read `0 newly-applied row(s) in the Job Radar
Tracker` as counting zero of the thing it was asked to touch. The old pattern
wanted the noun immediately after the zero and did not allow `row(s)`.

### What is actually wrong with 15 Part Job Search

Run properly, `py -m apply.main auto` exits 0 and prints
`0 newly-applied row(s) in the Job Radar Tracker`. It queries the Job Radar
Tracker for rows with `Date Applied` filled and subtracts the page ids in
`data/notion_sync_state.json`, which already holds 32 of them. Every row that
qualifies has been consumed, so the criterion — "verified against a real Job
Radar Tracker row" — cannot be met by that command alone. It needs a fresh
application, or an id cleared out of the state file to replay one, or a
criterion that does not require a live row. Story #1 is parked in `needs-info`
with that question on the card.

## Finished 2026-08-25 — a patch that fits, refused for a reason of git's own

Applying ticket #75 failed with `job-search/assisted-apply/PROJECT.md: does not
match index`. The patch was fine. `git apply --check` accepted it against the
working tree on the first try.

Both passes `apply_patch` had were index-aware. `--index` writes the index, and
`--3way` reads it to find the blobs it merges from, so both refuse when a file
the patch touches is `MM` in `git status` — staged with one content, on disk
with another. PROJECT.md was exactly that: a staged edit from earlier work plus
an unstaged tick of the `NOTION_OG_TRACKER_DB` checkbox. That combination says
nothing about whether the patch applies, and it is the PO's ordinary state in
assisted-apply, so every patch touching that project was going to be refused.

There is now a plain `git apply` between the two. It is as strict as the first
pass — all-or-nothing, every context line matched against what is on disk —
and it simply does not involve the index. The result lands unstaged, which is
where an applied patch was going anyway, and both the story event and the
card's reply say so, because `git diff --cached` is not the whole picture when
The PO already had work staged on the same file.

The three-way merge stays last, where it belongs: it is the only pass that can
write conflict markers, so it should be the one nothing else could replace.

## Finished 2026-08-25 — the completed panel reads its own fields

Opening a completed dispatch showed two rows of `[object Object]` under SKIPPED
and 294 rows of a single letter each under RISKS. The panel rendered every
findings field with one helper that walks an array and prints each element, and
only three of those fields are arrays of strings. `skipped` is a list of
`{criterion, why}` objects, so each one stringified to `[object Object]`.
`risks` and `learned` are single strings, and a `for..of` loop over a string
hands back its characters, so a 294-character risk note became 294 rows.

The helper now refuses anything that is not an array. `skipped` is flattened to
`criterion — why` first, matching how the approval card already renders it,
and `risks` and `learned` go through `sectionBlock` as the prose they are.
`needs_run` was not rendered at all and now is, as `command — why`: the
commands an agent handed over rather than ran are half the explanation for a
skipped criterion, and the card that carried them is closed by the time anyone
opens this panel.

## Finished 2026-08-25 — a run that comes back green is not a run that proved anything

Ticket #68 skipped the same two criteria ticket #62 skipped, and the reason it
gave was correct: a build agent has no shell, so it cannot run
`py -m apply.main auto` against live Notion. That is what `needs_run` is for,
and the agent used it properly — two commands, each with the criterion it
answers and a careful description of what a correct result looks like. Both
cards were approved. Both commands ran. Both were recorded as `exit 0`. Neither
one proved a thing.

```
$ py -m apply.main auto        exit 0    Notion query failed (ConnectionError)
$ py test_local.py weekly      exit 0    0 passed, 0 failed
```

Two separate faults, and the second is the worse one.

**Exit codes were the only evidence.** The agent wrote a precise `expect`
sentence for each command — the exact output line that would mean the sync
worked — and the colony put it on the card, showed it to the PO once, and threw
it away. What went into the story was the exit code. A program that catches its
own network error and returns 0 is completely ordinary, and the record said the
criterion had been answered.

`runner.judge` now reads the output. It returns `clean`, `suspect` or `failed`,
and `suspect` is the one that matters: exit 0 with a traceback in it, or a zero
count of the thing the command was supposed to touch, or the words *failed*,
*error*, *not set*, *could not*. Counts of zero are blanked first, so a suite
that prints "12 passed, 0 failed" stays clean. A `suspect` run is written to the
story as a `blocked` event, not a `finding`, and the transcript carries the
agent's `expect` text directly above the output so the next agent compares them
instead of guessing. `clean` still does not mean the criterion is met. It means
nothing in the output contradicts the request.

**A run-request was answerable before its own patch existed.**
`py test_local.py weekly` tests the `weekly` command that ticket #68 wrote. That
command was in an unapplied patch. Running it against the live tree tested a
version of the project that predates the work, found nothing, printed
"0 passed, 0 failed", and exited 0. A test that never ran, recorded as green.

`decide()` now refuses to approve a `run-request` while its ticket still has an
open `write-approval`. The card is not closed and not dismissed — it stays in
the Inbox and becomes answerable the moment the patch lands or is rejected. The
patch goes first, always, because a command that verifies the patch has nothing
to verify until then.

Both events on story #1 were relabelled to match what actually happened, and a
note on the story says why. The two criteria are still open, which is the
correct state: the OG-tracker sync has not been seen working against a live row,
and the `ConnectionError` behind that is the next thing to chase.

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
| story | a story filed `done`, `shipped` or `shelved` | when the PO files it |

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
its own run closes, and `settled_as`, which is the PO's word for a story and
stays their.

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
no ticket, no card. It sat there until the PO asked why nothing happened.

There are three lanes now, and the one a story takes turns on whether its
criteria were ever approved:

| story on unblock | lane | why |
| --- | --- | --- |
| no criteria | `backlog` | never groomed; grooming finds it there |
| criteria, never approved | `needs-criteria`, criteria cleared | drafted without the answer they just gave |
| criteria they approved | `ready` | their approval stands; the question only interrupted it |

The third row is the one that matters. Clearing criteria the PO approved would
make them approve the same list a second time, so the story goes back to the lane
the question interrupted and waits for Dispatch, which is their call and stays their
call.

One more thing came out of the same trace. `decisions_since` used
`resolved_at > since`, where `since` is the previous pulse's `pulse_at` — and
`pulse_at` is stamped during the tick, before the wake runs. Every decision the
wake itself resolved landed on that exact second and was excluded from the next
window forever. It is `>=` now. The 20:53:52 decision was invisible to the 21:07
beat for precisely this reason.

Story #1 was repaired by hand to match: escalation #31 resolved, the story back
in `ready` with its approved criteria, and a timeline note saying why.

## Finished 2026-08-24 — the colony writes the way the PO reads

The PO enabled the `unslop` writing-style skill for their own sessions and
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
one thing they can do in under two minutes.

It goes into all five prompts that ask a colonist for prose — grooming, the
build work order, the hiring decision, an Ordis reply, and a skill draft — and
in each one it sits immediately above the JSON contract, so the last thing the
agent reads before the field list is how to fill the fields in. That costs
about 350 tokens per prompt. It is worth it: a report the PO does not read
wastes the whole run, and a build run costs four figures.

Nothing here can stop a model writing badly. It can only say what good looks
like, in the words the PO uses on themselves.

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
and stopped, so nine files the PO had staged in `job-search/assisted-apply` and
not committed were absent, and the agent reported truthfully that
`PROPOSAL_next_steps.md` does not exist. `worktree.seed` now copies in every
tracked file that differs from HEAD repo-wide, plus every untracked file inside
the agent's scope folders, and records the result with `git write-tree`. The
diff is taken against that tree rather than HEAD, so the PO's own uncommitted
work does not come back in the patch as though an agent had written it. No
commit is involved: a tree object is not a commit and master is untouched. One
untracked file over 2 MB is skipped and the work order says how many were.

**A contract can be allowed to read the credential files.** `.env` is git-ignored
in every project here, so a worktree never contained one, and an agent asked
whether `NOTION_API_KEY` is set answered that it is not — which was wrong, and
was the second thing the PO asked about. `agents.sees_secrets` is off by
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
The PO's decision. `colony/runner.py` refuses `git push`, `git commit`, a forced
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
the projects root before storing it, refuses `..`, absolute paths, dot
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
bullets they could not see. Nothing was wrong with the display; the text was
already gone by the time it reached the page. The cap is now
`control.card_text`, which keeps 8000 characters, cuts at a word if it ever has
to cut at all, and puts an ellipsis there so a cut is visible as a cut. Story
#1's live card was repaired from `stories.acceptance_criteria`, which had kept
the whole thing. `_event` shortens a summary with `_gist` now instead of
slicing at 400, for the same reason.

**Ordis confirmed something they had no way to check.** They wrote "NOTION_OG_TRACKER_DB
is set in .env.example with the database ID — it's live now, not just logging
'not set'." The key is set, but in `.env`, which is outside their read scope; they
read the committed template beside it and reported one as the other. "It's live
now" they could not observe at all — their contract denies Bash, so they cannot run a
program and cannot see one run. The next groom then wrote acceptance criteria on
top of both claims. `reply_prompt` now states plainly what Read, Grep and Glob
can establish and what they cannot, forbids the words live, running, working,
fixed and verified, and names the `.env` / `.env.example` trap. The reply JSON
gained a `checked` list: the files they actually opened to support `settled`. If it
comes back empty, `answer_po` records the settled line as "the PO says: ...
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
had changed under them, one of them with conflict markers in it. The `does not exist in index`
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
deciding that for them:

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
The PO's, in Notion or on a button, and nowhere else.

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
The PO work that was its own.

**Images pasted into the chat are read now.** They always could be — attachments live on
disk and go to agents as absolute paths, and a live `claude -p --allowedTools Read` describes
one correctly. But only `reply_prompt` ever mentioned them, and only the ones on the message
being answered; `groom_prompt` and `build_prompt` mentioned them nowhere. Story 1 carries
three screenshots, and the groom that asked the PO for “the exact field list ... it was
never transcribed into text anywhere” was holding a prompt that did not know they existed.
**It asked them for a picture they had already sent.** `attachments.for_story` collects every
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
hire them with write scope” was asking them to read a roster of 270 people to do the Scrum
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

Ten things the PO asked for after living with M3 for a day. All shipped.

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
