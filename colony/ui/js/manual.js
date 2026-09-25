// The manual.

import { $, el } from "./core.js";
import { openDrawer } from "./drawer.js";

// ── the manual ──────────────────────────────────────────────────────────────
// How the colony works, in plain language, for the PO.
//
// A **frozen document**, not a live view: it reads nothing from the ledger and
// will drift. The date is when it was true. When it is wrong, rewrite it whole.
// Data rather than markup, since the page never uses innerHTML.
export const MANUAL_AS_OF = "2026-08-22";

export const MANUAL = [

{ h: "What this program is",
  p: ["Colony Dash runs a small team of Claude agents on the projects in your projects folder. You file the work as stories, either with the + Story button on the Board or from a Notion database if you keep one. Once an hour this program reads those stories, looks at your disk and your token budget, and decides whether there is anything worth doing. If there is, it does one small piece of it and asks you to approve the result.",
      "Everything it has ever done is written to one file: colony-dash/.colony/ledger.db. This page is a view of that file and nothing else. If something on screen looks wrong, the ledger is the thing to check, and the SQL console is the way to check it."],
  dl: [["You", "The Product Owner. You decide what gets built, when a project is finished, and whether any tokens are spent. Four things cannot happen without you clicking a button."],
       ["Ordis", "The Scrum Master. It reads your briefs, drafts acceptance criteria, proposes who to hire, and brings you questions. It never decides what to build and it never decides that something is finished."],
       ["The agents", "Hired one at a time for one job. Each gets one folder it may write to and is shut down when the job ends."]] },

{ h: "The hourly cycle",
  p: ["This is the part worth understanding first, because almost everything else hangs off it."],
  dl: [["pulse", "The hourly event. One row in the pulses table, every hour, whether or not anything happened. A missing row means the schedule is broken, and that is the only thing a missing row can mean."],
       ["tick", "The free half of a pulse. Plain Python, no model, zero tokens. It syncs Notion, reads your usage figures, scans the project folders, and decides whether this hour is worth spending on. Most hours it decides no, and that is the design working."],
       ["wake", "The paid half. Ordis actually runs. This happens only when the tick found a reason and there is a job in the queue. A wake that finds its job list empty stands down and costs nothing."],
       ["beat", "Just a word for one pulse. Nothing in the code knows it."]],
  note: "A pulse labelled tick cost you nothing. A pulse labelled wake spent tokens. If a row says wake and shows zero tokens, the tick escalated and the wake then found nothing to do." },

{ h: "The words for work",
  dl: [["story", "One project. It comes from one row in your Notion database, and the Notion page is the source of truth for what it says. The colony copies it; it does not own it."],
       ["ticket", "One job on one story. Grooming is a ticket, building is a ticket, answering your reply is a ticket. Tickets are what actually get handed to an agent."],
       ["run", "One execution of one agent against one ticket. This is where tokens are spent, and every run records what it cost."],
       ["groom", "Reading a brief and turning it into a numbered list of acceptance criteria, plus a list of what the brief does not say. This is the step that decides whether a story can be built at all."],
       ["build", "Writing the code. It happens inside a throwaway git worktree, never in your real folder, and it produces a patch you review."],
       ["harvest", "Collecting the result of a finished run into the ledger. An unharvested run is one that finished while nothing was watching."],
       ["sprint", "One week of budget. It runs Friday 05:00 to Friday 05:00 because that is when your Anthropic allowance resets, not because a week starts on a Friday."]] },

{ h: "The words for decisions",
  dl: [["card", "One question in your Inbox. Called an escalation in the code. Every card has buttons, and nothing behind it moves until you press one."],
       ["gate", "A point where the loop stops and waits for you. There are four of them, listed further down."],
       ["dismiss", "The x on a card. It closes the question without answering it, and the ledger records that you gave no answer rather than pretending you gave one."],
       ["stale", "A card whose story has been edited in Notion since the question was written. The question may no longer make sense, so it is set aside rather than answered."]] },

{ h: "The words for people",
  dl: [["roster", "270 personas on file. Job descriptions, not employees. Nobody on the roster costs anything."],
       ["agent", "A persona that has actually been hired, given a role name and a write scope. Only agents can be handed tickets."],
       ["standby", "A hired agent with nothing to do. It stays on the books and costs nothing while idle."],
       ["write scope", "The one folder an agent may modify. It is set when the agent is hired and cannot be widened by the agent."],
       ["skill", "A reusable instruction file the colony writes for itself. Candidates appear in the Forge panel. Drafting one costs tokens, so it waits for you."]] },

{ h: "The words for money",
  dl: [["token", "The unit of everything. Dollars are shown next to tokens but tokens are what the budget is kept in."],
       ["chargeable tokens", "Input plus output plus cache writes. This is the number that counts against your allowance, and it is larger than the output count you might expect."],
       ["allowance", "The share of your weekly Anthropic window the colony is allowed to use. The sprint sets a baseline and you can move it up or down for one week."],
       ["ceiling", "A per-job estimate. When a build looks like it will cost more than the ceiling, the loop stops and asks you before spending it."],
       ["halt", "The stop switch. No new work starts. Anything already running finishes, because the colony cannot kill a child process mid-sentence. The dashboard keeps updating so you can see what is going on."]] },

{ h: "The words for places",
  dl: [["Notion", "Where you write projects. The colony reads every row once an hour. It only ever writes to Notion when you press a button that says it will."],
       ["ledger", "colony-dash/.colony/ledger.db. Every story, ticket, run, card, and pulse, forever. Nothing is deleted."],
       ["project folder", "One directory under your projects folder. A story has to be matched to one before any writing can happen, and you confirm the match yourself."],
       ["worktree", "A temporary git checkout where a build happens. Your real folder is untouched until you approve the patch."],
       ["patch", "The diff a build produced. Approving it copies the files into your working tree, uncommitted. The colony never commits and never pushes."]] },

{ h: "The lanes on the board",
  p: ["A story sits in exactly one lane. The lane is the colony's own idea of where the work stands, and it is separate from the Status you set in Notion."],
  dl: [["backlog", "Seen, and the Notion row says In Progress, so it is work you want. Nothing has been read closely yet. Leaves when a wake grooms it."],
       ["needs criteria", "Waiting for a groom. Same as backlog in practice; it is where a story lands when its criteria have been cleared and it needs reading again."],
       ["needs info", "A groom ran and could not finish. The brief does not say something the writer needs. There will be a card in your Inbox with the specific question."],
       ["po review", "Ordis drafted acceptance criteria and is waiting for you to approve them. This is gate two. Nothing gets built from unapproved criteria."],
       ["ready", "You approved the criteria. The story can be staffed and built. It leaves when an agent is hired and dispatched."],
       ["running", "An agent is working on it right now."],
       ["delivered", "You approved a patch and the files are in your working tree, uncommitted. This does not mean the project is finished. If you then add more to the Notion page, the story goes back to needs criteria on the next pulse and the work continues."]],
  note: "Finished is a separate thing entirely, and only you can set it. Set the Notion Status to Done or Shipped, or press the button in the story drawer. Those stories leave the board and appear under done, shelved, or not started." },

{ h: "The life of a story, start to finish",
  ol: ["You write a page in the Notion database and set its Status to In Progress. Nothing else you set matters to the loop; In Progress is the only word that means work on this.",
       "Within the hour, a tick reads it and creates a story in the backlog. If the colony cannot tell which project folder it belongs to, it raises a card asking you. That is gate one, and it exists because the folder is what later authorizes writing to disk.",
       "A wake grooms it. Ordis reads the whole brief and either drafts acceptance criteria or writes down exactly what is missing. Either way it costs tokens and either way it produces a card.",
       "You approve the criteria. That is gate two. The story becomes ready. If the criteria are wrong, reject them and the story goes back for another read with the attempt counter reset.",
       "A wake proposes someone to hire from the 270 personas, with a role name and a write scope. That is gate three. Rejecting is a real answer and the next pulse proposes someone else.",
       "The agent builds, in a worktree, against the criteria you approved. If it looks like it will cost more than the ceiling, you get asked first.",
       "You review the patch and approve or discard it. That is gate four. Approving copies the files into your working tree, uncommitted, and the story becomes delivered. You commit it yourself.",
       "If the project is not finished, add the next part to the same Notion page. The next pulse notices the page changed, clears the old criteria, and puts the story back in needs criteria. The cycle repeats on the same story.",
       "When you are actually finished, set the Notion Status to Done or Shipped. Only then does anything call it done."] },

{ h: "The four gates",
  p: ["These are the four places the loop stops and waits for you. Everything else it does on its own."],
  ol: ["Which project folder does this story belong to? Answering this is what turns a guess into a permission.",
       "Are these acceptance criteria right? Nothing is built from criteria you have not approved.",
       "Should I hire this person for this job, with this write scope?",
       "Here is the patch. Apply it or throw it away?"],
  note: "There is a fifth stop that is not a gate: if a job is going to cost more than its ceiling, you are asked to approve the spend." },

{ h: "What the colony may never do",
  ul: ["Write outside your projects folder, ever.",
       "Write to any folder other than the one named by the ticket it is working on.",
       "Write to disk at all before you approve the patch.",
       "Touch .env files, credentials, or anything inside .git.",
       "Run git commit, git push, force-push, or delete a branch.",
       "Move a story into a lane that reads as finished, or write a Status back to Notion. Ending things is yours alone.",
       "Spend anything while production is halted."] },

{ h: "Things that look wrong, and what they actually mean",
  dl: [["The pulse log has a gap", "The hourly job did not run. It is a Windows Task Scheduler task called Colony Dash Pulse. Run python -m colony schedule --show to see its state, or python -m colony schedule to reinstall it. A gap is never the colony deciding to skip an hour; every hour writes a row."],
       ["A pulse says wake but spent nothing", "The tick found a reason to escalate, then the wake looked at its job list and found it empty. The drawer labels these escalated. They are free."],
       ["Acceptance criteria appeared and you do not know why", "Open the story and read its history. The groomed event holds Ordis's own summary of what it understood, and the full criteria are the detail underneath it. The ticket behind it holds the raw answer, including what Ordis thought was missing. If the reasoning is not good enough, reject the criteria; that sends it back for a fresh read rather than arguing with the old one."],
       ["A story says delivered but is not finished", "Delivered means one patch was approved, nothing more. Add the next part to the Notion page and the story comes back automatically."],
       ["Nothing is being built", "Check, in this order: is production halted, is the weekly allowance already spent, is there a card in the Inbox waiting on you, and is any story actually in ready. A story in po review is waiting for you, not for the colony."],
       ["A number looks stale", "The token figures come from the tray app's cache file, read fresh on every refresh. If the strip says cache stale, the tray app has stopped and the number on screen is old."]] },

{ h: "Commands worth knowing",
  dl: [["python -m colony pulse", "Run a pulse right now instead of waiting for the hour."],
       ["python -m colony pulse --dry-run", "Show what a pulse would do without writing a row."],
       ["python -m colony schedule --show", "Is the hourly task installed and when did it last run."],
       ["python -m colony sql \"SELECT ...\"", "Read the ledger directly. SELECT only."],
       ["python -m colony halt \"reason\"", "Stop all spending now. Resume lifts it."],
       ["python -m colony allowance 10", "Give this week ten more points of your weekly window. Zero clears it."],
       ["python -m colony agents", "Who is hired and what each may write to."]] },

];

export function manualSection(sec, id) {
  const box = el("section", "man-sec");
  box.id = id;
  box.append(el("h4", null, sec.h));
  (sec.p || []).forEach((t) => box.append(el("p", null, t)));
  if (sec.dl) {
    const dl = el("dl", "man-dl");
    sec.dl.forEach((pair) => dl.append(el("dt", null, pair[0]), el("dd", null, pair[1])));
    box.append(dl);
  }
  ["ol", "ul"].forEach((tag) => {
    if (!sec[tag]) return;
    const list = el(tag, "man-list");
    sec[tag].forEach((t) => list.append(el("li", null, t)));
    box.append(list);
  });
  if (sec.note) box.append(el("p", "man-note", sec.note));
  return box;
}

export function openManual() {
  const body = openDrawer("manual", "How the Colony Works", { wide: true });
  body.replaceChildren();
  body.append(el("p", "man-asof",
    "Written " + MANUAL_AS_OF + ". This is a snapshot, not a live view. Nothing here "
    + "reads the ledger, so it will drift as the code changes. When it is wrong, "
    + "delete it and write it again."));
  const toc = el("nav", "man-toc");
  MANUAL.forEach((sec, i) => {
    const jump = el("button", "link", sec.h);
    jump.onclick = () => {
      const target = $("man-" + i);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    toc.append(jump);
  });
  body.append(toc);
  MANUAL.forEach((sec, i) => body.append(manualSection(sec, "man-" + i)));
}
