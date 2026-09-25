// Standby: browsing the roster by division and adding a persona.

import { $, act, el, toast } from "./core.js";
import { openDivisions } from "./render.js";
import { blk, closeDrawer, getJSON, openDrawer } from "./drawer.js";
import { openPersona } from "./drawers.js";

// ── standby: browse by division ─────────────────────────────────────────────
// The roster, browsed by division as an accordion, biggest first.

export function renderDivisions(roster) {
  const box = $("roster-divisions");
  box.replaceChildren();
  for (const d of roster.divisions) {
    const det = el("details", "div");
    det.open = openDivisions.has(d.division);
    det.addEventListener("toggle", () => {
      det.open ? openDivisions.add(d.division) : openDivisions.delete(d.division);
    });
    const sum = el("summary");
    const hired = d.people.filter((p) => p.hired).length;
    sum.append(el("span", "dn", d.division.replace(/-/g, " ")),
               el("span", "cn", hired ? `${hired} hired / ${d.n}` : String(d.n)));
    det.append(sum);
    const people = el("div", "people");
    for (const p of d.people) people.append(personaButton(p));
    det.append(people);
    box.append(det);
  }
}

export function personaButton(p) {
  const b = el("button", "persona" + (p.hired ? " hired" : ""));
  b.append(el("span", null, p.emoji || "·"), el("span", "nm", p.name),
           el("span", "dv", p.hired ? "hired" : (p.source === "local" ? "yours" : "")));
  b.title = p.description || "";
  b.onclick = () => openPersona(p.slug);
  return b;
}

// ── adding a persona ────────────────────────────────────────────────────────
// Add a persona from the dashboard instead of hand-writing YAML frontmatter.
//
// Everything written here lands in `~/.colony-agents`, never in
// `~/.agency-agents`, which is someone else's git clone; the two are scanned
// together. Dropping a `.md` file and typing both fill the same form, so the
// division is always checked before writing.

export function parseFrontmatter(text) {
  // The same line reader as `roster.parse_persona`, so the import accepts only
  // what the scanner will show.
  const out = { body: text, meta: {} };
  if (!text.startsWith("---")) return out;
  const rest = text.slice(text.indexOf("\n") + 1);
  const end = rest.indexOf("\n---");
  if (end < 0) return out;
  for (const line of rest.slice(0, end).split("\n")) {
    const i = line.indexOf(":");
    if (i < 0) continue;
    const key = line.slice(0, i).trim().toLowerCase();
    if (["name", "description", "color", "emoji", "vibe"].includes(key)) {
      out.meta[key] = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
    }
  }
  out.body = rest.slice(end + 4).replace(/^\n+/, "");
  return out;
}

export async function openPersonaNew() {
  const body = openDrawer("standby", "Add a Persona");
  let dirs = { divisions: [], local_dir: "", agency_dir: "" };
  try { dirs = await getJSON("/api/roster/divisions"); } catch (_) {}
  body.replaceChildren();

  body.append(el("div", "note",
    "Written to " + (dirs.local_dir || "your personas folder") + ", not into the "
    + "agency-agents clone. So a git pull there can never clobber it, and "
    + "nothing you add here becomes part of the Colony Dash repo."));

  const form = el("div", "form");

  // A file input as well as a drop target, because a drop target alone is
  // unusable on a phone and this dashboard is used from one.
  const drop = el("div", "dropzone", "drop a persona .md here, or tap to pick one");
  const file = el("input");
  file.type = "file";
  file.accept = ".md,.markdown,text/markdown,text/plain";
  file.style.display = "none";
  drop.onclick = () => file.click();
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (ev) => {
    ev.preventDefault();
    drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, () => {
    drop.classList.remove("over");
  }));

  const fields = {};
  const field = (key, label, hint, tag) => {
    const input = el(tag || "input", "field");
    input.placeholder = hint || "";
    fields[key] = input;
    return blk(label, input);
  };

  // The division is required. A datalist: existing divisions are suggestions,
  // not a closed set.
  const division = el("input", "field");
  division.placeholder = "engineering, finance, your-own-department…";
  division.setAttribute("list", "roster-divisions-list");
  const list = el("datalist");
  list.id = "roster-divisions-list";
  for (const d of dirs.divisions || []) {
    const o = el("option");
    o.value = d.division;
    o.label = d.n + (d.mine ? " (" + d.mine + " yours)" : "");
    list.append(o);
  }
  fields.division = division;

  form.append(drop, file);
  form.append(blk("division", division, list,
    el("div", "note", "Each division becomes a folder. Pick one of yours, "
      + "or type a new department and it gets created.")));
  form.append(field("name", "name", "Project Shepherd"));
  form.append(field("slug", "file name", "leave blank to use the name"));
  form.append(field("description", "description",
    "one line. This is what the hiring prompt reads.", "textarea"));
  form.append(field("vibe", "vibe", "optional. One line, shown on the card."));

  const short = el("div", "row");
  const emoji = el("input", "field");
  emoji.placeholder = "🐑";
  emoji.style.maxWidth = "6em";
  const color = el("input", "field");
  color.placeholder = "blue";
  color.setAttribute("list", "roster-colors-list");
  const colors = el("datalist");
  colors.id = "roster-colors-list";
  for (const c of ["red", "orange", "yellow", "green", "teal", "blue", "purple",
                   "pink", "brown", "grey"]) {
    const o = el("option");
    o.value = c;
    colors.append(o);
  }
  fields.emoji = emoji;
  fields.color = color;
  short.append(emoji, color, colors);
  form.append(blk("emoji and colour", short,
    el("div", "note", "both optional. They are the face on the Standby card.")));

  const bodyBox = el("textarea", "field tall");
  bodyBox.placeholder = "# Who they are\n\nYou are …\n\n## How they work\n\n…";
  fields.body = bodyBox;
  form.append(blk("the persona itself", bodyBox,
    el("div", "note", "Markdown. Headings become the sections shown on the "
      + "persona card. This is the part an agent is actually given; the "
      + "frontmatter above is only how it gets found.")));

  const replace = el("input");
  replace.type = "checkbox";
  const replaceRow = el("label", "row");
  replaceRow.append(replace, el("span", null,
    " replace a persona of mine with the same file name"));
  form.append(replaceRow);

  const fill = (text, filename) => {
    const parsed = parseFrontmatter(text);
    for (const key of ["name", "description", "emoji", "color", "vibe"]) {
      if (parsed.meta[key]) fields[key].value = parsed.meta[key];
    }
    bodyBox.value = parsed.body;
    if (filename) {
      const stem = filename.replace(/\.mdx?$/i, "");
      fields.slug.value = stem;
      if (!fields.name.value) fields.name.value = stem;
    }
    drop.textContent = (filename ? "loaded " + filename : "loaded")
      + ", check the division, then add";
    if (!division.value) division.focus();
  };
  const take = (f) => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => fill(String(reader.result || ""), f.name);
    reader.readAsText(f);
  };
  drop.addEventListener("drop", (ev) => {
    ev.preventDefault();
    take(ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]);
  });
  file.onchange = () => take(file.files && file.files[0]);

  const save = el("button", "act", "add persona");
  save.onclick = async () => {
    if (!fields.name.value.trim()) { toast("a persona needs a name", "bad"); return; }
    if (!division.value.trim()) { toast("pick or type a division", "bad"); return; }
    save.disabled = true;
    const out = await act("persona", {
      division: division.value,
      slug: fields.slug.value || fields.name.value,
      name: fields.name.value,
      description: fields.description.value,
      emoji: emoji.value,
      color: color.value,
      vibe: fields.vibe.value,
      body: bodyBox.value,
      overwrite: replace.checked,
    });
    save.disabled = false;
    if (!out) return;                       // `act` has already said why
    toast("added. " + out.total + " personas on file");
    closeDrawer();
  };
  form.append(save);
  body.append(form);
}

$("roster-add").onclick = openPersonaNew;
$("roster-rescan").onclick = async () => {
  const out = await act("rescan", {});
  if (out) {
    toast(out.total + " personas (" + out.local + " yours), "
      + out.added.length + " added, " + out.changed.length + " changed, "
      + out.removed.length + " gone");
  }
};
