# Colony Dash

**Status:** M0 to M5 shipped and in daily use. The hourly pulse is live (939 beats logged
through 2026-09-25), 86 agent runs so far, 17 of them write-capable. Published at
github.com/GoodTimesPM/colony-dash by `../publish-colony-dash.ps1`.
Priority: High.
**Created:** 2026-08-15 as "PO Dashboard", renamed Colony Dash 2026-08-17.

The orchestrator loop and dashboard where the PO acts as Product Owner over a colony of
Claude agents, with Ordis as Scrum Master. It is the execution side of the Notion idea
[Mimic Scrum Environment](https://app.notion.com/p/3a6e98f012738047bccdf1671a127948).

- `ARCHITECTURE.md` is the one-page design; `docs/design.md` is the full one.
- `ROSTER.md` covers where agents come from and how they get hired.
- `README.md` is the manual, including every CLI command.
- `docs/history/project-log.md` is this file's old journal through 2026-09-04, and
  `docs/history/build-log.md` is the milestone log. Decisions and their reasons live there.

## Running it

```
python -m colony dash          open the dashboard window
python -m colony dash --serve  serve only, browse 127.0.0.1:8787
python -m colony pulse         one heartbeat, free unless it wakes
python -m colony status        the text view
```

Tests: `.venv/Scripts/python.exe -m unittest discover -s tests -t .` from `colony-dash/`
(384 tests, one skipped). Ledger: `.colony/ledger.db`. Config: `.env`. Both gitignored.

## Recent

- **2026-09-25, audit fixes.** A 31-finding code and UI audit, fixed across ~45 commits.
  The 5,800-line `app.js` is now ES modules under `colony/ui/js/`, and `server.py` is
  FastAPI routers under `colony/web/`. Security: phone pairing uses one-time codes, served
  attachments are sandboxed, build write scope is enforced at apply time. Correctness:
  budget checked before every wake step, whole process trees killed on timeout, stale
  pulse locks freed. UI: one shared snapshot per interval, only changed panels redraw,
  clickable cards are buttons, live headings, short empty states with a "?" for the
  long version, pixel avatars instead of emoji in Standby. Long card copy lives in
  `colony/wording.py`. This file and ARCHITECTURE.md were cut from ~160 KB each.
  Audit: https://claude.ai/artifact/9ZfwDPa4Fwdq8v6F1vKFSx
- **2026-09-15.** Teams per story and a second-opinion button; the PO can change what a
  story is for; restart replaces a running dashboard instead of starting a second one;
  double-click install from the zip.

## Next

- [ ] **Restart the dashboard** to pick up the 2026-09-25 Python changes. The running
      server is serving the older code.
- [ ] **Answer the Inbox.** Five stories sit in needs-info, two in PO review ("15 Part
      Job Search", "Mimic Scrum Environment"), and one decision card is open.
- [ ] Tune the escalation bar (`docs/design.md` §4.6) against three weeks of real logs.
- [ ] Code comments run at about 0.25 comment lines per code line against a 0.2 target.
      Left there on purpose after the history trim; cut further only where a comment
      restates the code.
- [ ] The manual (`colony/ui/js/manual.js`) is a frozen snapshot from 2026-08-22. Rewrite
      it from the code once the workflow settles, rather than patching it line by line.
