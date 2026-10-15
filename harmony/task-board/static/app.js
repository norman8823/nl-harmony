"use strict";

/* Board UI. Every value that came from a ticket is written with textContent or
 * a validated href, so ticket text can never become markup or a script. */

const STAGES = ["Next", "In Progress", "Blocked", "Done"];
// The canonical stage "Next" is shown as To Do; the stored value stays "Next".
const STAGE_LABELS = { Next: "To Do" };
const STATUSES = ["BACKLOG", "READY", "INVESTIGATING", "IMPLEMENTING", "REVIEW", "BLOCKED", "DONE"];
const REOPEN_STATUSES = ["BACKLOG", "READY"];
const UNKNOWN = "unknown";
// board.verification is a human check of the card against its ticket record and evidence, not testing.
const RECORD_TITLES = {
  verified: "Record checked: someone confirmed this card against its ticket record and evidence. " +
    "A board check only; not tests, review or software verification.",
  unknown: "Record unchecked: nobody has confirmed this card against its ticket record and evidence yet. " +
    "A board check only; not tests, review or software verification.",
};

function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage;
}

const lanesEl = document.getElementById("lanes");
const statusEl = document.getElementById("status");
const dialog = document.getElementById("editor");
const errorEl = document.getElementById("editor-error");

let cards = [];
let editing = null; // {mode: "edit"|"create", card}

function say(text, kind = "ok") {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
  statusEl.dataset.kind = kind;
}

function isUnknown(value) {
  return !value || String(value).trim().toLowerCase() === UNKNOWN;
}

function field(node, value) {
  const unknown = isUnknown(value);
  node.textContent = unknown ? "unknown" : value;
  node.classList.toggle("is-unknown", unknown);
}

const META_SOURCES = { approval_request: "from the approval request", blocker: "from the ticket blocker",
  checkpoint: "from the checkpoint handoff", graph: "from the task graph", status: "from the ticket status",
  missing: "no owner recorded" };

function metaField(node, shown, stored) {
  if (!shown) return field(node, stored);
  field(node, shown.value);
  node.classList.toggle("is-unknown", shown.source !== "board");
  node.title = META_SOURCES[shown.source] || "";
}

function tag(text, kind) {
  const span = document.createElement("span");
  span.className = "tag" + (kind ? " " + kind : "");
  span.textContent = text;
  return span;
}

/* Only http(s) ever becomes a real link. Anything else is rendered as copyable
 * text, so javascript:, data: and file: targets cannot be clicked. */
function safeHref(target) {
  try {
    const url = new URL(target, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch (_) {
    return null;
  }
}

function renderLink(link) {
  const href = link.external ? safeHref(link.target) : null;
  if (href) {
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.textContent = link.label || link.target;
    anchor.rel = "noreferrer noopener";
    anchor.target = "_blank";
    anchor.title = link.target;
    return anchor;
  }
  const span = document.createElement("span");
  span.className = "path";
  span.textContent = (link.label && link.label !== link.target ? link.label + " — " : "") + link.target;
  span.title = "Click to copy this path";
  span.addEventListener("click", () => {
    navigator.clipboard?.writeText(link.target).then(
      () => say("Copied: " + link.target),
      () => say("Could not copy; select the text instead.", "error")
    );
  });
  return span;
}

function renderDefect(defect) {
  const box = document.createElement("div");
  box.className = "defect";
  const label = "Linked defect: Issue #" + defect.id + " (" + (defect.state || UNKNOWN) + ")";
  const href = defect.url ? safeHref(defect.url) : null;
  if (href) {
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.textContent = label;
    anchor.rel = "noreferrer noopener";
    anchor.target = "_blank";
    box.append(anchor);
  } else {
    box.append(document.createTextNode(label));
  }
  const why = document.createElement("span");
  why.className = "why";
  why.textContent = (defect.title || UNKNOWN) + (defect.note ? " — " + defect.note : "");
  box.append(why);
  return box;
}

function renderCard(card) {
  const node = document.getElementById("card-template").content.firstElementChild.cloneNode(true);
  const board = card.board;
  const meta = card.meta || {};
  const completion = card.completion || null;
  // Every DONE card (automatic, manual or legacy): resolved blockers, satisfied gates and old
  // next steps are left to Inspect, not deleted; what still matters is a current canonical badge.
  const settled = Boolean(completion);
  node.dataset.id = card.id;
  node.dataset.stage = board.stage;
  node.dataset.verification = board.verification === "verified" ? "verified" : UNKNOWN;

  node.querySelector(".id").textContent = card.display;
  node.querySelector(".title").textContent = card.title || UNKNOWN;

  const flags = node.querySelector(".flags");
  const badge = (text, kind, title) => {
    const span = tag(text, kind);
    if (title) span.title = title;
    flags.append(span);
  };
  badge("status " + card.status);
  if (completion) {
    badge(completion.label, completion.verified || completion.kind === "codex" ? "tag-complete" : "unknown",
      [completion.detail].concat(completion.reasons || []).join("\n"));
    const pending = completion.pending_authorization || [];
    if (pending.length) badge(pending.join(", ") + " not authorized", "tag-gate",
      (completion.reasons || []).filter((r) => pending.some((a) => r.includes(a))).join("\n"));
  }
  // Separate from completion and approvals: a human check of the card against its record.
  badge(board.verification === "verified" ? "Record checked" : "Record unchecked", "tag-record",
    RECORD_TITLES[board.verification === "verified" ? "verified" : "unknown"]);
  if (board.intake_notice) badge("unassessed at intake", "tag-gate", "At intake: " + board.intake_notice);
  if (board.derived) flags.append(tag("no board metadata yet", "derived"));
  // Badge class is deliberately NOT "gate": the badge sits in .flags, before the
  // labelled <dd class="gate">, so a bare ".gate" lookup would select the badge
  // and write the full gate text into it, leaving the labelled field empty.
  if (!settled && !isUnknown(board.gate)) flags.append(tag("gate", "tag-gate"));
  if (meta.approval && !(settled && meta.approval === "approved")) {
    badge("approval " + meta.approval.replace(/_/g, " ") + ": " + meta.approval_action,
      meta.approval === "stale" ? "unknown" : "tag-approval");
  }

  // Unknown board fields fall back (read only) to the ticket's approval request, blocker,
  // checkpoint or graph; the source is in the tooltip and nothing is written back.
  metaField(node.querySelector(".next-action"), meta.next_action, board.next_action);
  metaField(node.querySelector(".owner"), meta.owner, board.owner);
  metaField(node.querySelector("dd.gate"), meta.gate, board.gate);
  node.querySelector(".fields").hidden = settled;

  // Completed Tasks only: the server sends run totals computed by the shared
  // run-record summary, and only the ones it could compute safely.
  const metrics = node.querySelector(".run-metrics");
  if (card.observability && card.observability.line) {
    metrics.hidden = false;
    metrics.textContent = card.observability.line;
    metrics.title = card.observability.basis || "";
  }

  const links = node.querySelector(".links");
  (board.links || []).forEach((link) => links.append(renderLink(link)));
  const defects = node.querySelector(".defects");
  (board.linked_defects || []).forEach((defect) => defects.append(renderDefect(defect)));

  const select = node.querySelector(".move-select");
  STAGES.forEach((stage) => {
    const option = document.createElement("option");
    option.value = stage;
    option.textContent = stageLabel(stage);
    option.selected = stage === board.stage;
    select.append(option);
  });
  select.addEventListener("change", () => move(card, select.value, select));
  const lifecycleButton = node.querySelector(".lifecycle");
  const isDone = card.status === "DONE";
  lifecycleButton.textContent = isDone ? "Reopen" : "Mark Done";
  lifecycleButton.title = isDone
    ? "Reopen as BACKLOG or READY; no grant, approval or graph change"
    : "Manual closure: administrative only; no review, approval, release, commit, merge or publication is recorded";
  lifecycleButton.addEventListener("click", () => lifecycle(card, isDone ? "reopen" : "done", lifecycleButton));
  // Archive is visibility only, for completed tasks. An archived card always offers Restore,
  // even once reopened, and keeps Reopen, Edit, Move and Inspect.
  const archiveButton = node.querySelector(".archive");
  archiveButton.hidden = !isDone && !board.archived;
  archiveButton.textContent = board.archived ? "Restore" : "Archive";
  archiveButton.title = board.archived ? "Show this task on the board again"
    : "Hide this completed task in Archived; its ticket, id, history and approvals stay as they are";
  archiveButton.addEventListener("click", () => lifecycle(card, board.archived ? "restore" : "archive", archiveButton));
  inspectLink(node.querySelector(".inspect"), card.id);
  node.querySelector(".edit").addEventListener("click", () => openEditor(card));
  return node;
}

function sortCards(list) {
  return list.slice().sort((a, b) => {
    const priority = (c) => (typeof c.board.priority === "number" ? c.board.priority : 500);
    if (priority(a) !== priority(b)) return priority(a) - priority(b);
    return a.id - b.id;
  });
}

let showArchived = false; // the Archived view inside Tasks

function archivable() {
  return cards.filter((c) => c.status === "DONE" && !c.board.archived);
}

function render() {
  const visible = cards.filter((c) => Boolean(c.board.archived) === showArchived);
  const archivedCount = cards.filter((c) => c.board.archived).length;
  const toggle = document.getElementById("show-archived");
  toggle.textContent = showArchived ? "Back to board" : "Archived (" + archivedCount + ")";
  toggle.setAttribute("aria-pressed", String(showArchived));
  const bulk = document.getElementById("archive-done");
  bulk.hidden = showArchived;
  bulk.disabled = !archivable().length;
  // Park the existing button before rebuilding lanes, retaining its listener.
  document.getElementById("panel-kanban").append(bulk);
  lanesEl.classList.toggle("archive-view", showArchived);
  lanesEl.replaceChildren();
  (showArchived ? ["Archived"] : STAGES).forEach((stage) => {
    const inLane = showArchived ? visible.slice().sort((a, b) => b.id - a.id)
      : sortCards(visible.filter((c) => c.board.stage === stage));
    const lane = document.createElement("section");
    lane.className = "lane";
    lane.dataset.stage = stage;
    const heading = document.createElement("h2");
    heading.textContent = stageLabel(stage);
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = inLane.length;
    heading.append(count);
    const body = document.createElement("div");
    body.className = "lane-body";
    if (inLane.length === 0) {
      const empty = document.createElement("p");
      empty.className = "lane-empty";
      empty.textContent = showArchived ? "No archived tasks." : "No cards.";
      body.append(empty);
    }
    inLane.forEach((card) => body.append(renderCard(card)));
    lane.append(heading, body);
    if (stage === "Done") {
      const actions = document.createElement("div");
      actions.className = "lane-actions";
      actions.append(bulk);
      heading.after(actions);
    }
    lanesEl.append(lane);
  });
}

async function api(path, options) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options && options.headers) },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    /* fall through to a status-based message */
  }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || "request failed with HTTP " + response.status);
    error.payload = payload || {};
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function load(message) {
  say(message || "Loading…", "busy");
  try {
    const payload = await api("/api/board");
    cards = payload.cards;
    const add = document.getElementById("new-card");
    add.disabled = false;
    add.title = payload.create_mode === "intake"
      ? "Allocated through this repo's intake allocator (BLOCKED until assessed)."
      : "Creates a BACKLOG ticket in .workflow/tickets; nothing is authorized or started.";
    render();
    const counts = STAGES.map((s) => stageLabel(s) + " " + payload.counts[s]).join(" · ");
    say(cards.length + " tasks — " + counts + (payload.archived ? " · Archived " + payload.archived : ""));
  } catch (error) {
    say("Could not load the board: " + error.message, "error");
  }
}

async function move(card, stage, select) {
  if (stage === card.board.stage) return;
  // Done follows the canonical status: moving there is Mark Done (a labelled
  // manual closure), and a DONE card leaves Done only by Reopen.
  if (stage === "Done") return lifecycle(card, "done", select);
  if (card.status === "DONE") {
    select.value = card.board.stage;
    say(card.display + " is DONE; Reopen it before moving it.", "error");
    return;
  }
  select.disabled = true;
  try {
    const result = await api("/api/tasks/" + card.id, {
      method: "PATCH",
      body: JSON.stringify({ stage, expected_revision: card.board.revision }),
    });
    replace(result.card);
    say("Moved " + card.display + " to " + stageLabel(stage) + ". Saved.");
  } catch (error) {
    select.value = card.board.stage;
    say(error.message, "error");
    if (error.payload && error.payload.stale) await load("Reloading after a stale edit…");
  } finally {
    select.disabled = false;
  }
}

/* Mark Done / Reopen / Archive / Restore write only the ticket; nothing is started, merged or published. */
const LIFECYCLE_DONE = {
  done: (card) => card.display + " marked Done: manual closure only; no review, approval or release recorded. Saved.",
  reopen: (card, updated) => card.display + " reopened as " + updated.status + ". Saved.",
  archive: (card) => card.display + " archived; restore it from Archived. Saved.",
  restore: (card) => card.display + " restored to the board. Saved.",
};
async function lifecycle(card, action, control) {
  control.disabled = true;
  try {
    const result = await api("/api/tasks/" + card.id + "/" + action, {
      method: "POST",
      body: JSON.stringify({ expected_revision: card.board.revision }),
    });
    replace(result.card);
    say(LIFECYCLE_DONE[action](card, result.card));
  } catch (error) {
    if (control.tagName === "SELECT") control.value = card.board.stage;
    say(error.message, "error");
    if (error.payload && error.payload.stale) await load("Reloading after a stale edit…");
  } finally {
    control.disabled = false;
  }
}

function replace(updated) {
  cards = cards.map((c) => (c.id === updated.id ? updated : c));
  render();
}

/* Archive every completed card shown on the board, all or none, each at the revision on screen. */
async function archiveCompleted(button) {
  const done = archivable();
  if (!done.length) return;
  button.disabled = true;
  try {
    const result = await api("/api/tasks/archive", {
      method: "POST",
      body: JSON.stringify({ tasks: done.map((c) => ({ id: c.id, expected_revision: c.board.revision })) }),
    });
    result.cards.forEach((updated) => { cards = cards.map((c) => (c.id === updated.id ? updated : c)); });
    render();
    say("Archived " + result.cards.length + " completed task" + (result.cards.length === 1 ? "" : "s")
      + "; restore any from Archived. Saved.");
  } catch (error) {
    say("Nothing archived: " + error.message, "error");
    if (error.payload && error.payload.stale) await load("Reloading after a stale edit…");
  } finally {
    button.disabled = !archivable().length;
  }
}

/* ---- editor ---- */

function linksToText(links) {
  return (links || []).map((link) => (link.label && link.label !== link.target ? link.label + " | " : "") + link.target).join("\n");
}

function textToLinks(text) {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const cut = line.lastIndexOf("|");
      const label = cut > 0 ? line.slice(0, cut).trim() : "";
      const target = cut > 0 ? line.slice(cut + 1).trim() : line;
      const external = /^https?:\/\//i.test(target);
      return { label: label || target, target, kind: external ? "pr" : "evidence" };
    });
}

function openEditor(card) {
  editing = card ? { mode: "edit", card } : { mode: "create", card: null };
  const board = card ? card.board : null;
  document.getElementById("editor-title").textContent = card ? "Edit " + card.display : "Add a task";
  document.getElementById("editor-hint").textContent = card
    ? "Title, status and details are saved on the ticket with a history entry. Authorization, " +
      "blocker, approvals, review, routing and acceptance are not changed here. Status DONE is a " +
      "manual closure: it records no review, approval or release."
    : "A new task is saved as a ticket in BACKLOG with no authorization, acceptance criteria or " +
      "review. Nothing is started. Any gate you enter is kept as your own.";
  document.getElementById("editor-note").textContent = card
    ? "Leave a board field empty to record it as unknown."
    : "Only a title is required.";

  const title = document.getElementById("f-title");
  title.value = card ? card.title : "";
  title.disabled = false;
  title.required = true;
  const titleNote = document.getElementById("f-title-note");
  const original = card && (card.original_title || (card.board.display_title ? card.ticket_title : null));
  titleNote.hidden = !original;
  // The ticket title is edited in place; the first title is kept on the ticket and in its history.
  if (original) titleNote.textContent = "Original title: \u201c" + original + "\u201d (kept in the ticket history).";

  const status = document.getElementById("f-status");
  status.replaceChildren();
  const statuses = !card ? ["BACKLOG"]
    : card.status === "DONE" ? ["DONE"].concat(REOPEN_STATUSES)
    : (STATUSES.includes(card.status) ? [] : [card.status]).concat(STATUSES);
  statuses.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name === "DONE" ? "DONE (manual closure)" : name;
    option.selected = card ? name === card.status : name === "BACKLOG";
    status.append(option);
  });
  status.disabled = !card;
  document.getElementById("f-details").value = card ? card.details || "" : "";

  const stage = document.getElementById("f-stage");
  stage.replaceChildren();
  STAGES.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = stageLabel(name);
    option.selected = board ? name === board.stage : name === "Next";
    stage.append(option);
  });
  stage.disabled = !card;

  document.getElementById("f-owner").value = board && !isUnknown(board.owner) ? board.owner : "";
  document.getElementById("f-next").value = board && !isUnknown(board.next_action) ? board.next_action : "";
  document.getElementById("f-gate").value = board && !isUnknown(board.gate) ? board.gate : "";
  document.getElementById("f-gate").disabled = false;
  document.getElementById("f-notes").value = board ? board.notes || "" : "";
  const linksField = document.getElementById("f-links");
  linksField.value = board ? linksToText(board.links) : "";
  linksField.disabled = !card;
  document.getElementById("f-verified").checked = Boolean(board && board.verification === "verified");
  document.getElementById("f-verified").disabled = !card;
  errorEl.hidden = true;
  dialog.showModal();
  (card ? document.getElementById("f-next") : title).focus();
}

async function save() {
  const button = document.getElementById("editor-save");
  button.disabled = true;
  errorEl.hidden = true;
  try {
    if (editing.mode === "create") {
      const result = await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: document.getElementById("f-title").value,
          details: document.getElementById("f-details").value,
          owner: document.getElementById("f-owner").value,
          next_action: document.getElementById("f-next").value,
          gate: document.getElementById("f-gate").value,
          notes: document.getElementById("f-notes").value,
        }),
      });
      dialog.close();
      await load("Created " + result.card.display + " (" + result.card.status + "). Saved.");
      return;
    }
    const card = editing.card;
    const body = {
      expected_revision: card.board.revision,
      stage: document.getElementById("f-stage").value,
      owner: document.getElementById("f-owner").value.trim() || UNKNOWN,
      next_action: document.getElementById("f-next").value.trim() || UNKNOWN,
      gate: document.getElementById("f-gate").value.trim() || UNKNOWN,
      notes: document.getElementById("f-notes").value,
      links: textToLinks(document.getElementById("f-links").value),
      verification: document.getElementById("f-verified").checked ? "verified" : UNKNOWN,
    };
    // Canonical fields are sent only when changed, so an untouched field is never rewritten.
    const title = document.getElementById("f-title").value.trim();
    if (title !== card.title) body.title = title;
    const details = document.getElementById("f-details").value.trim();
    if (details !== (card.details || "")) body.details = details;
    const status = document.getElementById("f-status").value;
    if (status !== card.status) body.status = status;
    const result = await api("/api/tasks/" + card.id, { method: "PATCH", body: JSON.stringify(body) });
    dialog.close();
    replace(result.card);
    say("Saved " + card.display + " (revision " + result.card.board.revision + ").");
  } catch (error) {
    errorEl.hidden = false;
    errorEl.textContent = error.message;
    if (error.payload && error.payload.stale) {
      errorEl.textContent += " The board has been refreshed behind this dialog.";
      await load("Reloading after a stale edit…");
    }
  } finally {
    button.disabled = false;
  }
}


document.getElementById("editor-save").addEventListener("click", save);
// Stage and status agree in the editor as on the server: the Done lane is status DONE and only DONE.
document.getElementById("f-status").addEventListener("change", (event) => {
  const stage = document.getElementById("f-stage");
  if (event.target.value === "DONE") stage.value = "Done";
  else if (stage.value === "Done") stage.value = "Next";
});
document.getElementById("f-stage").addEventListener("change", (event) => {
  const status = document.getElementById("f-status");
  if (status.disabled || !editing.card) return;
  if (event.target.value === "Done") status.value = "DONE";
  else if (status.value === "DONE") status.value = editing.card.status === "DONE" ? REOPEN_STATUSES[0] : editing.card.status;
});
document.getElementById("editor-cancel").addEventListener("click", () => dialog.close());
document.getElementById("new-card").addEventListener("click", () => openEditor(null));
document.getElementById("reload").addEventListener("click", () => load());
document.getElementById("show-archived").addEventListener("click", () => { showArchived = !showArchived; render(); });
document.getElementById("archive-done").addEventListener("click", (event) => archiveCompleted(event.currentTarget));

/* ---- tabs: Observability (default) and Tasks (the board; tab id and #kanban route kept) ---- */

const TABS = { observability: "panel-observability", kanban: "panel-kanban" };
let activeTab = "observability";
let kanbanLoaded = false;

/* Shows a tab and returns its load. The hash follows the tab (replaced, not
 * pushed), so Back from Inspect and a reload both land on the same tab. */
function showTab(name) {
  activeTab = name;
  closeInspectView();
  document.body.dataset.view = name;
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  Object.entries(TABS).forEach(([tab, panel]) => {
    const button = document.getElementById("tab-" + tab);
    const selected = tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    document.getElementById(panel).hidden = !selected;
  });
  if (name === "kanban") {
    refreshRunState();
    kanbanLoaded = true;
    return load();
  }
  if (quotaDue()) loadQuota();
  return loadObservability();
}

document.querySelectorAll('[role="tab"]').forEach((button) => {
  button.addEventListener("click", () => showTab(button.id.replace("tab-", "")));
  button.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const next = activeTab === "observability" ? "kanban" : "observability";
    showTab(next);
    document.getElementById("tab-" + next).focus();
  });
});

/* ---- observability: measured values only; null renders as "unknown" ---- */

function fmtTokens(n) {
  if (n === null || n === undefined) return UNKNOWN;
  for (const [unit, size] of [["B", 1e9], ["M", 1e6], ["k", 1e3]]) {
    if (n >= size) return (n / size).toFixed(1) + unit;
  }
  return String(Math.round(n));
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return UNKNOWN;
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  if (h) return h + "h " + String(m).padStart(2, "0") + "m";
  if (m) return m + "m " + String(s).padStart(2, "0") + "s";
  return s + "s";
}

function fmtWhen(iso) {
  if (!iso) return "never";
  const at = new Date(iso);
  if (isNaN(at)) return iso;
  const ago = Math.max(0, Date.now() - at.getTime());
  return at.toLocaleTimeString() + " (" + fmtDuration(ago) + " ago)";
}

function el(name, className, text) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function value(text, unknown) {
  const span = el("span", unknown ? "is-unknown" : "val", text);
  return span;
}

function kv(label, text, title) {
  const row = el("div", "kv");
  row.append(el("span", "k", label));
  const v = value(text, text === UNKNOWN || text === "never");
  if (title) v.title = title;
  row.append(v);
  return row;
}

/* The plottable points of one measure on a time axis: measured values with a
 * valid timestamp, in time order, each with its position (0..1) by elapsed
 * time. Points without a valid time are counted, not placed; fewer than two
 * distinct times give no axis (null). */
function sparkTimes(series, key) {
  const measured = (series || []).filter((p) => typeof p[key] === "number");
  const timed = measured.map((p) => ({ p, ms: Date.parse(p.t) })).filter((q) => !isNaN(q.ms))
    .sort((a, b) => a.ms - b.ms);
  if (timed.length < 2) return null;
  const t0 = timed[0].ms, span = timed[timed.length - 1].ms - t0;
  if (span <= 0) return null;
  return { points: timed.map((q) => ({ ...q.p, pos: (q.ms - t0) / span })), t0, span,
    untimed: measured.length - timed.length };
}

/* One single-series sparkline per measure (never two scales on one axis).
 * x is elapsed time between measurements, not their sequence number.
 * Each point carries a <title>, so hovering shows its time and value. */
function sparkline(series, key, label) {
  const axis = sparkTimes(series, key);
  if (!axis) return null;
  const points = axis.points;
  const svgNS = "http://www.w3.org/2000/svg";
  const width = 180, height = 36, pad = 4;
  const values = points.map((p) => p[key]);
  const lo = Math.min(...values), hi = Math.max(...values);
  const x = (i) => pad + points[i].pos * (width - 2 * pad);
  const y = (v) => height - pad - (hi === lo ? 0.5 : (v - lo) / (hi - lo)) * (height - 2 * pad);
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label + ": " + points.length + " measurements over " + fmtDuration(axis.span)
    + ", latest " + fmtTokens(values[values.length - 1]));
  const line = document.createElementNS(svgNS, "polyline");
  line.setAttribute("points", points.map((p, i) => x(i).toFixed(1) + "," + y(p[key]).toFixed(1)).join(" "));
  line.setAttribute("class", "spark-line");
  svg.append(line);
  points.forEach((p, i) => {
    const hit = document.createElementNS(svgNS, "circle");
    hit.setAttribute("cx", x(i).toFixed(1));
    hit.setAttribute("cy", y(p[key]).toFixed(1));
    hit.setAttribute("r", i === points.length - 1 ? "3" : "6");
    hit.setAttribute("class", i === points.length - 1 ? "spark-end" : "spark-hit");
    const title = document.createElementNS(svgNS, "title");
    title.textContent = label + " " + fmtTokens(p[key]) + " at " + new Date(p.t).toLocaleTimeString()
      + " (+" + fmtDuration(p.pos * axis.span) + ")";
    hit.append(title);
    svg.append(hit);
  });
  const figure = el("figure", "spark-figure");
  figure.append(el("figcaption", null, label + " · " + lo.toLocaleString() + "–" + hi.toLocaleString()
    + " · over " + fmtDuration(axis.span) + " from " + new Date(axis.t0).toLocaleTimeString()
    + (axis.untimed ? " · " + axis.untimed + " without a time not plotted" : "")), svg);
  return figure;
}

function telemetryBlock(t, heading) {
  const box = el("div", "telemetry");
  if (heading) box.append(el("h4", null, heading));
  if (!t) {
    box.append(el("p", "is-unknown", "No telemetry in this record (recorded before live telemetry existed)."));
    return box;
  }
  const ctx = t.context || {};
  let ctxText = UNKNOWN;
  if (ctx.tokens !== null && ctx.tokens !== undefined) {
    ctxText = fmtTokens(ctx.tokens) + " tokens";
    ctxText += ctx.window ? " of " + fmtTokens(ctx.window) + " (" + ctx.percent + "%)" : " · window unknown";
  }
  box.append(kv("Context", ctxText, ctx.basis));
  const cum = t.cumulative || {};
  let cumText = UNKNOWN;
  if (cum.processed_input !== null && cum.processed_input !== undefined || cum.output !== null && cum.output !== undefined) {
    cumText = "in " + fmtTokens(cum.processed_input) + " · out " + fmtTokens(cum.output)
      + (cum.reasoning_output != null ? " (reasoning " + fmtTokens(cum.reasoning_output) + ")" : "")
      + (cum.provisional ? " · provisional" : "");
  }
  box.append(kv("Cumulative", cumText, cum.basis));
  const raw = cum.scope_unverified;
  if (raw) {
    // Reported by the provider but not this invocation's own usage: shown apart, never as cumulative.
    box.append(kv("Reported, scope unverified", "in " + fmtTokens(raw.processed_input) + " · out " + fmtTokens(raw.output)
      + " (latest of " + raw.turns + " turn" + (raw.turns === 1 ? "" : "s") + ")", cum.basis));
  }
  if (cumText === UNKNOWN && cum.basis) box.append(el("p", "hint", cum.basis));
  box.append(kv("Last measured", fmtWhen(t.last_measured_at), t.measured_at_basis || "nothing measured yet"));
  box.append(kv("Last event", fmtWhen(t.last_event_at) + (t.phase ? " · " + t.phase : ""), (t.events || 0) + " events read"));
  if ((t.models_seen || []).length) box.append(kv("Models reported", t.models_seen.join(", ")));
  const sparks = el("div", "sparks");
  [sparkline(t.series, "context_tokens", "Context tokens"),
   sparkline(t.series, "processed_input", "Cumulative input"),
   sparkline(t.series, "output", "Cumulative output")].forEach((f) => f && sparks.append(f));
  if (sparks.childElementCount) box.append(sparks);
  return box;
}

function table(headers, rows) {
  const tableEl = el("table", "obs-table");
  const head = el("tr");
  headers.forEach((h) => head.append(el("th", null, h)));
  tableEl.append(el("thead"));
  tableEl.tHead.append(head);
  const body = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((cell) => {
      const td = el("td", cell === UNKNOWN ? "is-unknown" : null, cell);
      tr.append(td);
    });
    body.append(tr);
  });
  tableEl.append(body);
  return tableEl;
}

function renderPhases(phases) {
  const strip = el("ol", "phases");
  if (!phases) {
    strip.append(el("li", "is-unknown", "no task graph"));
    return strip;
  }
  phases.forEach((phase) => {
    const item = el("li", "phase phase-" + (phase.state || "unknown"));
    item.append(el("strong", null, phase.id), el("span", null, " " + (phase.state || UNKNOWN)));
    const runs = (phase.runs || []).map((r) => r.status + (r.total_tokens != null ? " " + fmtTokens(r.total_tokens) + " tok" : "")
      + (r.duration_ms != null ? " " + fmtDuration(r.duration_ms) : ""));
    if (runs.length) item.append(el("span", "phase-runs", " · " + runs.join(", ")));
    item.title = (phase.role || "") + (phase.model ? " · " + phase.model : "")
      + (phase.started_at ? " · started " + phase.started_at : "") + (phase.completed_at ? " · completed " + phase.completed_at : "");
    strip.append(item);
  });
  return strip;
}

function renderTaskObs(view) {
  const section = el("article", "obs-task");
  const head = el("div", "obs-head");
  head.append(el("h3", null, "Task #" + view.task_id + (view.title ? " — " + view.title : "")));
  if (view.status) head.append(tag("status " + view.status));
  section.append(head);
  section.append(renderPhases(view.phases));

  view.live.forEach((run) => {
    const live = el("div", "live-run" + (run.state === "interrupted" ? " interrupted" : ""));
    const model = run.model_requested ? "requested " + run.model_requested : "model unknown";
    const state = run.state === "running" && !isConfirmedRun(run) ? "running (unconfirmed)" : run.state;
    live.append(el("h4", null, (run.state === "interrupted" ? "■ " : "▶ ") + run.role_label + " · " + run.provider + " · " + model
      + " · " + state + (run.elapsed_ms != null ? " " + fmtDuration(run.elapsed_ms) : "")));
    live.title = run.state_basis + (run.active ? " · " + run.active.basis : "") + " · run " + run.run_id;
    live.append(telemetryBlock(run.telemetry));
    section.append(live);
  });

  const summary = view.summary;
  if (summary.run_count) {
    section.append(el("h4", null, "Completed runs"));
    section.append(table(
      ["Role", "Provider", "Model", "Duration", "Input", "Output", "Context at end", "Est. list cost (not a charge)",
        "Outcome"],
      summary.runs.map((run) => {
        const t = (view.telemetry || {})[run.run_id] || {};
        const ctx = t.context || {};
        const input = [run.tokens.uncached_input, run.tokens.cache_creation_input, run.tokens.cached_input];
        const processed = input.some((v) => v === null || v === undefined) ? null : input.reduce((a, b) => a + b, 0);
        return [
          run.role_label, run.provider,
          (run.model_reported && run.model_reported.length ? run.model_reported.join(" + ") : (run.model ? run.model + " (requested)" : UNKNOWN)),
          fmtDuration(run.duration_ms.value), fmtTokens(processed), fmtTokens(run.tokens.output),
          ctx.tokens != null ? fmtTokens(ctx.tokens) + (ctx.percent != null ? " (" + ctx.percent + "%)" : "") : UNKNOWN,
          typeof run.estimated_list_cost_usd === "number" ? "$" + run.estimated_list_cost_usd.toFixed(4) : UNKNOWN,
          (run.outcome_cli || UNKNOWN) + (run.outcome_review ? " / review " + run.outcome_review : ""),
        ];
      })
    ));
    // Each completed run's final telemetry (context, cumulative, time series), collapsed.
    summary.runs.forEach((run) => {
      const t = (view.telemetry || {})[run.run_id];
      if (!t) return;
      const details = el("details", "run-telemetry");
      details.dataset.runId = run.run_id;
      details.append(el("summary", null, "Telemetry · " + run.role_label + " · " + run.run_id));
      details.append(telemetryBlock(t));
      section.append(details);
    });
    const attribution = summary.attribution || {};
    // Every counter, unknown ones included, as recorded per invocation.
    const counters = (g) => [fmtTokens(g.tokens.uncached_input), fmtTokens(g.tokens.cached_input),
      fmtTokens(g.tokens.cache_creation_input), fmtTokens(g.tokens.output),
      typeof g.cached_share === "number" ? fmtShare(g.cached_share) : UNKNOWN, fmtTokens(g.total)];
    const counterHeads = ["Fresh input", "Cache read", "Cache write", "Output", "Cached share", "Input + output"];
    section.append(el("h4", null, "Usage by provider and model"));
    section.append(table(["Provider", "Model", "Runs", ...counterHeads, "Reasoning (subset of output)"],
      (attribution.by_provider_model || []).map((g) => [g.provider,
        g.model ? (g.model_name ? g.model_name + " (" + g.model + ")" : g.model)
          + (SOURCE_NOTES[g.model_source] || "") : UNKNOWN, String(g.runs.length),
        ...counters(g), g.reasoning_output != null ? fmtTokens(g.reasoning_output) : "not reported"])));
    section.append(el("h4", null, "Usage by role"));
    section.append(table(["Role", "Runs", ...counterHeads],
      (attribution.by_role || []).map((g) => [ROLE_LABELS[g.role] || g.role, String(g.runs.length), ...counters(g)])));
    const foot = el("p", "hint", "Task total: " + fmtTokens(summary.tokens.processed_input != null && summary.tokens.output != null
      ? summary.tokens.processed_input + summary.tokens.output : null) + " tokens · " + fmtDuration(summary.duration_ms.value)
      + ". " + (attribution.basis || "") + (attribution.cached_share_basis ? ". Cached share: " + attribution.cached_share_basis : "")
      + (attribution.subagents ? ". Subagents: " + attribution.subagents : "")
      + "." + (summary.unknowns.length ? " Unknown: " + summary.unknowns.join(", ") + "." : ""));
    section.append(foot);
  } else if (!view.live.length) {
    section.append(el("p", "is-unknown", "No completed runs recorded."));
  }
  return section;
}

const ROLE_LABELS = { writer: "Implementer", correction: "Correction", planner: "Planner", reviewer: "Reviewer",
  investigator: "Investigator", other: "Other" };


/* ---- overview: the current task, its confirmed active runs and its usage rows ---- */

const PROVIDERS = [
  { key: "codex", name: "Codex", roles: ["planner", "reviewer"], label: "Planner / Reviewer" },
  { key: "claude", name: "Claude", roles: ["writer", "correction"], label: "Implementer / Correction" },
];
const REASONS = { running: "Running now", active: "In progress", recent: "Most recent activity" };

function runTime(run) {
  const ms = Date.parse(run && run.started_at);
  return isNaN(ms) ? -Infinity : ms;
}

// Newest first; entries without a valid time tie and keep their given order.
function byNewest(a, b) {
  return (b.at - a.at) || 0;
}

function latestActivity(view) {
  return Math.max(-Infinity, ...view.live.map(runTime), ...view.summary.runs.map(runTime));
}

// A run proven in progress: its own recorder process, for this task and role (`active.confirmed`).
function isConfirmedRun(run) {
  return run.state === "running" && !!run.active && run.active.confirmed === true;
}

/* The overview's task, chosen deterministically: the task of the newest
 * confirmed running run; else an In Progress board card (newest recorded
 * activity, then the higher id); else the task with the newest recorded run.
 * An unconfirmed run never selects the task. A board card with no run
 * records can still be current. */
function chooseCurrentTask(views, boardCards) {
  views = views || [];
  boardCards = boardCards || [];
  const viewOf = (id) => views.find((v) => String(v.task_id) === String(id)) || null;
  const pick = (id, reason) => ({ id: String(id), reason, view: viewOf(id),
    card: boardCards.find((c) => String(c.id) === String(id)) || null });
  const running = views.flatMap((v) => v.live.filter(isConfirmedRun)
    .map((r) => ({ id: v.task_id, at: runTime(r) }))).sort(byNewest);
  if (running.length) return pick(running[0].id, "running");
  const active = boardCards.filter((c) => c.board && c.board.stage === "In Progress")
    .map((c) => ({ id: c.id, at: viewOf(c.id) ? latestActivity(viewOf(c.id)) : -Infinity }))
    .sort((a, b) => byNewest(a, b) || (Number(b.id) - Number(a.id)) || 0);
  if (active.length) return pick(active[0].id, "active");
  const recent = views.map((v) => ({ id: v.task_id, at: latestActivity(v) })).sort(byNewest);
  return recent.length ? pick(recent[0].id, "recent") : null;
}

/* "Review complete · Awaiting approval" only when the graph shows it: the last
 * review passed and release is still blocked or pending, with nothing running. */
function currentPhase(view) {
  const phases = (view && view.phases) || [];
  const now = phases.find((p) => p.state === "running")
    || phases.find((p) => p.state === "failed")
    || phases.find((p) => p.state === "blocked" && p.type !== "release");
  const reviews = phases.filter((p) => p.type === "review" && p.state === "completed");
  const release = phases.find((p) => p.type === "release");
  if (!now && reviews.length && reviews[reviews.length - 1].outcome === "pass" && release
      && (release.state === "blocked" || release.state === "pending")) return "Review complete · Awaiting approval";
  const shown = now || phases.find((p) => p.state === "blocked");
  if (shown) return "Phase " + shown.id + " · " + shown.state;
  const next = phases.find((p) => p.state !== "completed" && p.state !== "skipped");
  if (next) return "Next phase " + next.id;
  return phases.length ? "All phases completed" : null;
}

const PROVIDER_NAMES = { codex: "Codex", claude: "Claude" };

/* A model as reported: its readable name and its exact id; an id the provider
 * did not report (only requested) or a mixed, unsplit set says so. */
// How a usage group's model is known; reported ids need no note. Overview and Inspect share these.
const SOURCE_NOTES = { reported: "", unknown: "", requested: " (requested; not reported)", mixed: " (not split)" };

function modelLabel(group) {
  const id = group.model || UNKNOWN;
  const name = group.model_name && group.model_name !== id ? group.model_name + " · " + id : id;
  return name + (SOURCE_NOTES[group.model_source] || "");
}

function fmtShare(share) {
  return Math.round(share * 1000) / 10 + "%";
}

/* Usage parts of a row: only measured values, an unknown one is left out. */
function usageParts(group) {
  const t = group.tokens || {};
  const parts = [["fresh in", t.uncached_input], ["cache read", t.cached_input],
    ["cache write", t.cache_creation_input], ["out", t.output]]
    .filter(([, n]) => typeof n === "number").map(([label, n]) => label + " " + fmtTokens(n));
  if (typeof group.cached_share === "number") parts.push(fmtShare(group.cached_share) + " cached input");
  return parts;
}

const isCount = (n) => typeof n === "number" && Number.isFinite(n) && n >= 0;
const exact = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");

/* A group's measured input: the backend's input_total, else the same provider-specific sum of
 * known counters (Claude fresh + cache write + cache read; Codex fresh + cache read); else null. */
function groupInput(group) {
  if (isCount(group.input_total)) return group.input_total;
  if (!["claude", "codex"].includes(group.provider)) return null;
  const t = group.tokens || {};
  const names = group.provider === "codex" ? ["uncached_input", "cached_input"]
    : ["uncached_input", "cache_creation_input", "cached_input"];
  const parts = names.map((name) => t[name]);
  return parts.every(isCount) ? parts.reduce((a, b) => a + b, 0) : null;
}

/* Cache reuse over the groups whose cache reads and input are both measured: summed reads over
 * summed input (weighted), never an average of per-group percentages. Cache writes only when
 * every counted group measured them. Null when no group is measurable. */
function cacheReuse(groups) {
  const counted = groups.filter((g) => isCount((g.tokens || {}).cached_input) && isCount(groupInput(g)));
  const input = counted.reduce((sum, g) => sum + groupInput(g), 0);
  if (!input) return null;
  const reads = counted.reduce((sum, g) => sum + g.tokens.cached_input, 0);
  const writes = counted.every((g) => isCount(g.tokens.cache_creation_input))
    ? counted.reduce((sum, g) => sum + g.tokens.cache_creation_input, 0) : null;
  const left = groups.length - counted.length;
  return "Cache reuse: " + fmtShare(reads / input) + " of measured input"
    + (writes !== null ? " · cache writes " + fmtTokens(writes) : "")
    + (left ? " · " + left + " group" + (left === 1 ? "" : "s") + " without measured input or cache reads excluded" : "");
}

/* The current task's per-invocation usage over its completed, measured runs: one compact row
 * per provider, exact model and role, with a bar of its share of the recorded processed tokens
 * (input + output). The denominator is the sum of the known group totals only; a group without a
 * recorded total gets no bar (never a zero one) and is named as excluded. Not allowance usage. */
function usageRows(view) {
  const groups = (view && view.summary.attribution && view.summary.attribution.by_provider_model_role) || [];
  if (!groups.length) return null;
  const box = el("section", "usage");
  box.append(el("h3", null, "Token usage"));
  const known = groups.filter((g) => isCount(g.total));
  const denominator = known.reduce((sum, g) => sum + g.total, 0);
  const excluded = groups.length - known.length;
  box.append(el("p", "usage-caption", (denominator > 0
    ? "Shares of recorded processed tokens · input + output · completed runs"
    : "No recorded processed tokens to share out yet")
    + "."
    + (excluded ? " " + excluded + " group" + (excluded === 1 ? "" : "s") + " without a recorded total "
      + (excluded === 1 ? "is" : "are") + " excluded." : "")));
  const list = el("ul", "usage-rows");
  groups.forEach((group) => {
    const row = el("li", "usage-row");
    row.dataset.provider = group.provider;
    const who = el("p", "usage-who");
    who.append(el("span", "usage-provider", (PROVIDER_NAMES[group.provider] || group.provider) + " · "
      + (group.role_label || ROLE_LABELS[group.role] || group.role)),
    el("span", "usage-model", modelLabel(group)));
    const total = el("span", "usage-total", isCount(group.total) ? fmtTokens(group.total) : "");
    who.append(total);
    row.append(who);
    if (denominator > 0 && isCount(group.total)) {
      const share = group.total / denominator;
      const bar = el("div", "usage-bar");
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", fmtShare(share) + " share of recorded processed tokens");
      const fill = el("span", "usage-fill");
      fill.style.width = (share * 100) + "%";
      bar.append(fill);
      who.append(el("span", "usage-share", fmtShare(share)));
      row.append(bar);
    }
    const parts = usageParts(group);
    row.append(el("p", "usage-parts", parts.length ? parts.join(" · ") : "no usage reported"));
    const input = groupInput(group);
    const output = (group.tokens || {}).output;
    row.title = (isCount(group.total) ? exact(group.total) + " recorded processed tokens"
      + (isCount(input) && isCount(output) ? " (input " + exact(input) + " + output " + exact(output) + ")" : "")
      : "No recorded total; excluded from the shares") + " · " + group.runs.length + " completed run"
      + (group.runs.length === 1 ? "" : "s") + " · per-invocation usage";
    list.append(row);
  });
  box.append(list);
  const cache = cacheReuse(groups);
  if (cache) box.append(el("p", "usage-cache", cache));
  return box;
}

/* Runs proven in progress for this task (the recorder process itself, for
 * this task and role), one line per provider and role. A bare pid, a stale
 * record or any other process is never shown as active. */
function activeRuns(view) {
  const runs = ((view && view.live) || []).filter(isConfirmedRun);
  if (!runs.length) return null;
  const seen = new Map();
  runs.forEach((run) => {
    const key = run.provider + "/" + run.role;
    if (!seen.has(key) || runTime(run) > runTime(seen.get(key))) seen.set(key, run);
  });
  const list = el("ul", "active-runs");
  [...seen.values()].forEach((run) => {
    const item = el("li", null, "Running · " + (PROVIDER_NAMES[run.provider] || run.provider) + " "
      + (run.role_label || run.role) + (run.elapsed_ms != null ? " · " + fmtDuration(run.elapsed_ms) : ""));
    item.dataset.provider = run.provider;
    item.title = run.active.basis + " · run " + run.run_id;
    list.append(item);
  });
  return list;
}

/* ---- weekly allowance: account-wide, from official provider sources only ---- */

let quotaPayload = null;  // the last /api/quota answer
let quotaFailure = null;  // why the latest fetch failed, or null
let quotaAttemptAt = 0;   // Date.now() of the latest fetch attempt
const QUOTA_POLL_MS = 60000;
const QUOTA_STALE_S = 150; // a reading this old is shown as not fresh, unless its source says otherwise

function fmtCountdown(ms) {
  const m = Math.max(0, Math.floor(ms / 60000));
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), min = m % 60;
  if (d) return d + "d " + h + "h";
  if (h) return h + "h " + min + "m";
  return min + "m";
}

function fmtPercent(n) {
  return (Math.round(n * 10) / 10).toString() + "%";
}

function epochWhen(seconds) {
  return fmtWhen(new Date(seconds * 1000).toISOString());
}

function quotaFailureNote(error) {
  if (error.status === 404) return "this companion server predates weekly allowance; restart the companion";
  if (error.status) return "the companion server answered HTTP " + error.status;
  return "the companion server could not be reached";
}

/* pending | unavailable | fresh | stale | expired. A failed fetch never
 * leaves a reading looking fresh, and a passed reset removes the meter. */
function quotaState(reading) {
  if (!reading) return quotaFailure ? "unavailable" : "pending";
  const resetMs = reading.resets_at != null ? reading.resets_at * 1000 : null;
  if (reading.state === "expired" || (reading.available && resetMs !== null && resetMs <= Date.now())) return "expired";
  if (!reading.available) return reading.state === "pending" && !quotaFailure ? "pending" : "unavailable";
  // A source polled less often (Claude /usage: every 5 minutes) says how long its reading stays current.
  const staleAfter = typeof reading.stale_after_seconds === "number"
    ? Math.max(QUOTA_STALE_S, reading.stale_after_seconds) : QUOTA_STALE_S;
  if (quotaFailure || reading.state === "stale" || reading.refresh_error
      || Date.now() / 1000 - reading.measured_at > staleAfter) return "stale";
  return "fresh";
}

function quotaCard(provider, reading) {
  const box = el("article", "quota-card");
  box.dataset.provider = provider.key;
  box.append(el("h3", null, provider.name), el("p", "tagline", provider.label));
  const state = quotaState(reading);
  box.dataset.state = state;
  const note = (text) => box.append(el("p", "quiet quota-note", text));
  if (state === "pending") {
    note("Checking weekly allowance…");
    return box;
  }
  if (state === "unavailable") {
    note("Weekly allowance unavailable · " + (quotaFailure || (reading && reading.note) || "no reading"));
    return box;
  }
  const last = reading.last_measured_at || reading.measured_at;
  if (state === "expired") {
    note("Weekly window has reset · a new reading is needed");
    if (last) box.append(el("p", "measured", "Last measured " + epochWhen(last)));
    return box;
  }
  const remaining = reading.remaining_percent;
  const big = el("p", "big-number", fmtPercent(remaining));
  big.title = fmtPercent(reading.used_percent) + " used of the weekly window · " + reading.source;
  const meter = el("div", "meter");
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-label", provider.name + " weekly allowance left"
    + (state === "stale" ? " (last measured, not current)" : ""));
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", "100");
  meter.setAttribute("aria-valuenow", String(remaining));
  const fill = el("span", "meter-fill");
  fill.style.width = remaining + "%";
  meter.append(fill);
  box.append(big, el("p", "caption", "Weekly allowance left" + (state === "stale" ? " · last measured" : "")), meter);
  if (reading.resets_at != null) {
    const resetMs = reading.resets_at * 1000;
    const reset = el("p", "reset", "Resets in " + fmtCountdown(resetMs - Date.now()));
    reset.title = "Resets " + new Date(resetMs).toLocaleString();
    box.append(reset);
  }
  const why = quotaFailure || reading.refresh_error || "no newer reading";
  box.append(el("p", "measured", state === "fresh" ? "Measured " + epochWhen(last)
    : "Last measured " + epochWhen(last) + " · not refreshed: " + why));
  return box;
}

function renderQuota() {
  const providers = quotaPayload && quotaPayload.providers;
  document.getElementById("quota").replaceChildren(
    ...PROVIDERS.map((provider) => quotaCard(provider, providers ? providers[provider.key] : null)));
}

let quotaBusy = false;
async function loadQuota() {
  if (quotaBusy) return;
  quotaBusy = true;
  quotaAttemptAt = Date.now();
  try {
    quotaPayload = await api("/api/quota");
    quotaFailure = null;
  } catch (error) {
    // The last payload stays, shown as stale with its own measured time.
    quotaFailure = quotaFailureNote(error);
  } finally {
    quotaBusy = false;
    renderQuota();
  }
}

// Off the 3 s tick (tab switch, page shown again, startup): ask while the server's first reading
// is still pending, otherwise only when the last ask is a minute old.
function quotaPending() {
  const providers = quotaPayload && !quotaFailure ? quotaPayload.providers || {} : {};
  return Object.values(providers).some((reading) => reading && reading.state === "pending");
}
function quotaDue() {
  return quotaPending() || Date.now() - quotaAttemptAt >= QUOTA_POLL_MS;
}

function renderOverview(box, current, notes) {
  box.replaceChildren();
  notes.forEach((note) => box.append(el("p", "quiet", note)));
  if (!current) {
    box.append(el("p", "quiet overview-empty", "Nothing is running or in progress, and no runs are recorded yet."));
    return;
  }
  const view = current.view, card = current.card;
  const title = (card && card.title) || (view && view.title);
  const status = (card && card.status) || (view && view.status);
  const head = el("section", "current");
  head.dataset.reason = current.reason;
  const text = el("div", "current-text");
  const h2 = el("h2", null, "Task #" + current.id + (title ? " · " + title : ""));
  h2.title = REASONS[current.reason];
  text.append(h2);
  const line = currentPhase(view) || (status ? "Status " + status : null);
  if (line) text.append(el("p", "phase-line", line));
  head.append(text, inspectLink(el("a", "inspect-link", "Inspect ›"), current.id));
  box.append(head);
  [activeRuns(view), usageRows(view)].forEach((part) => part && box.append(part));
}

/* The header's run state covers every Harmony-managed run. It never says Idle
 * when a run cannot be checked or the records could not be read, and says
 * Paused, not a stale state, while live refresh is off. */
function runState(views) {
  if (!document.getElementById("obs-live").checked) return { state: "paused", text: "Paused",
    title: "Live refresh is off; turn it on to check runs again." };
  if (!views) return { state: "unknown", text: "Status unknown", title: "Run records could not be read." };
  const live = views.flatMap((v) => v.live);
  const count = (fn) => live.filter(fn).length;
  // Running only with the recorder proof; a live pid alone (maybe reused) is unconfirmed.
  const running = count(isConfirmedRun), interrupted = count((r) => r.state === "interrupted");
  const unconfirmed = live.length - running - interrupted;
  const title = [running + " running", unconfirmed + " cannot be confirmed", interrupted + " interrupted"].join(" · ");
  if (unconfirmed) return { state: "unconfirmed", text: running ? "Running · some unconfirmed" : "Cannot confirm", title };
  if (running) return { state: "running", text: "Running", title };
  if (interrupted) return { state: "interrupted", text: "Interrupted", title };
  return { state: "idle", text: "Idle", title: "No Harmony-managed run is running." };
}

function showRunState(views) {
  const s = runState(views);
  const node = document.getElementById("run-state");
  node.dataset.state = s.state;
  node.title = s.title + (s.state === "paused" ? "" : " · checked " + new Date().toLocaleTimeString());
  document.getElementById("run-state-text").textContent = s.text;
}

function obsViews(obs) {
  return obs.status === "fulfilled" && obs.value.available ? obs.value.tasks : null;
}

async function refreshRunState() {
  if (!document.getElementById("obs-live").checked) return showRunState(null);
  const [obs] = await Promise.allSettled([api("/api/observability")]);
  showRunState(obsViews(obs));
}

let obsBusy = false;
async function loadObservability() {
  if (obsBusy) return;
  obsBusy = true;
  const meta = document.getElementById("obs-meta");
  const box = document.getElementById("overview");
  try {
    const [obs, board] = await Promise.allSettled([api("/api/observability"), api("/api/board")]);
    showRunState(obsViews(obs));
    if (obs.status === "rejected" && board.status === "rejected") {
      meta.textContent = "Could not refresh: " + obs.reason.message;
      if (!box.childElementCount) box.append(el("p", "quiet", "The companion data could not be loaded."));
      return;
    }
    const notes = [];
    if (obs.status === "rejected") notes.push("Run records could not be loaded: " + obs.reason.message);
    else if (!obs.value.available) notes.push("Run records are not available in this installation.");
    if (board.status === "rejected") notes.push("The board could not be loaded: " + board.reason.message);
    const views = obsViews(obs) || [];
    renderQuota();
    renderOverview(box, chooseCurrentTask(views, board.status === "fulfilled" ? board.value.cards : []), notes);
    meta.textContent = "";
  } finally {
    obsBusy = false;
  }
}

/* ---- Inspect Task: one task's full report, a secondary view (#inspect/<id>) ---- */

let inspectId = null;   // the task shown while the view is open
let inspectFrom = null; // the tab it was opened from
let inspectBusy = false;

function inspectLink(node, id) {
  node.href = "#inspect/" + encodeURIComponent(id);
  node.dataset.inspect = String(id);
  node.setAttribute("aria-label", "Inspect task #" + id);
  node.addEventListener("click", () => { inspectFrom = activeTab; });
  return node;
}

function closeInspectView() {
  inspectId = null;
  inspectFrom = null;
  document.getElementById("inspect").hidden = true;
}

function showInspect(id) {
  const body = document.getElementById("inspect-body");
  if (inspectId !== id) {
    body.replaceChildren();
    delete body.dataset.task;
    document.getElementById("inspect-meta").textContent = "Loading…";
  }
  inspectId = id;
  document.body.dataset.view = "inspect";
  Object.values(TABS).forEach((panel) => { document.getElementById(panel).hidden = true; });
  document.getElementById("inspect-back").textContent = "← Back to "
    + (inspectFrom === "kanban" ? "Tasks" : "Observability");
  document.getElementById("inspect").hidden = false;
  const title = document.getElementById("inspect-title");
  title.textContent = "Inspect task #" + id;
  title.focus();
  return loadInspect();
}

function leaveInspect() {
  if (inspectId !== null) location.hash = "#" + (inspectFrom || "observability");
}

function taskWithoutRuns(card, note) {
  const section = el("article", "obs-task");
  const head = el("div", "obs-head");
  head.append(el("h3", null, "Task #" + card.id + (card.title ? " — " + card.title : "")));
  if (card.status) head.append(tag("status " + card.status));
  section.append(head, el("p", "quiet", note), ticketRecord(card));
  return section;
}

function shown(value) {
  if (value === null || value === undefined || value === "") return "none";
  if (typeof value === "object") return value.kind ? value.kind + " closure" : JSON.stringify(value);
  return String(value);
}

function historyLine(entry) {
  if (!entry || typeof entry !== "object") return String(entry);
  const changes = Object.entries(entry.changes || {}).map(([name, change]) =>
    name + ": " + shown(change && change.from) + " → " + shown(change && change.to));
  if ((entry.board_fields || []).length) changes.push("board " + entry.board_fields.join(", "));
  const action = entry.action === "manual_close" && entry.by === "codex-lifecycle"
    ? "Completed by Codex" : String(entry.action || "change").replace(/_/g, " ");
  return [entry.at || "time unknown", entry.by || UNKNOWN, action]
    .join(" · ") + (changes.length ? " — " + changes.join("; ") : "");
}

/* The canonical ticket as stored: details, status, closure and history. */
function ticketRecord(card) {
  const box = el("div");
  const fields = el("dl", "fields");
  const row = (label, text) => {
    const dd = el("dd");
    field(dd, text);
    fields.append(el("dt", null, label), dd);
  };
  row("Status", card.status);
  row("Details", card.details);
  if (card.closure) row("Closure", card.closure.label);
  if (card.completion) row("Completion", card.completion.label);
  if (card.closure && card.closure.note) row("Completion summary", card.closure.note);
  if (card.completion && (card.completion.reasons || []).length) {
    row("Not supported by the records", card.completion.reasons.join("; "));
  }
  const board = card.board || {};
  if (board.intake_notice) row("At intake", board.intake_notice);
  if (board.archived) row("Board", "Archived (visibility only; restore it from Archived on Tasks)");
  if (card.original_title) row("Original title", card.original_title);
  (card.relations || []).filter((r) => r.type === "follow_up_of").forEach((r) => row("Follow-up of", "Task #" + r.task_id));
  if ((card.followed_up_by || []).length) row("Followed up by", card.followed_up_by.map((id) => "Task #" + id).join(", "));
  box.append(fields);
  const approvals = approvalBlock(card);
  if (approvals) box.append(approvals);
  box.append(el("h4", null, "History"));
  const history = card.history || [];
  if (!history.length) box.append(el("p", "hint", "No history recorded on this ticket."));
  else {
    const list = el("ol", "hint");
    history.forEach((entry) => list.append(el("li", null, historyLine(entry))));
    box.append(list);
  }
  return box;
}

/* Approval requests recorded on the ticket. Only an existing, current, pending request gets
 * buttons, and a decision sends back exactly the request, task, gate, action and binding shown. */
// Request Changes feedback drafts, keyed by request id AND binding so a draft never carries over to
// another request (or a reissued one); they survive the 3 s refresh and are dropped once decided.
const approvalDrafts = {}; // key -> {open, text, error}
const approvalBusy = new Set(); // keys with a decision in flight: no duplicate submissions
let approvalResult = null; // {task, request, ok, headline, text}: what the last decision did and what is next
const FEEDBACK_LIMIT = 1000;
const DECIDED_HEADLINES = { approve: "Approved", request_changes: "Changes requested", defer: "Deferred",
  approved: "Approved", changes_requested: "Changes requested", deferred: "Deferred" };
const draftKey = (request) => request.id + "|" + request.binding_token;

/* What a decided request did, from its stored decision: shown on every reload, so buttons that are
 * gone are explained and feedback given with Request Changes is visible. Nothing is invented: an
 * older Request Changes without feedback says so. */
function decidedExplanation(request) {
  const d = request.decision || {};
  const box = el("div", "approval-decided");
  const who = " by " + (d.by || UNKNOWN) + (d.via ? " via " + d.via : "") + " at " + (d.at || UNKNOWN);
  box.append(el("p", null, (DECIDED_HEADLINES[request.state] || String(request.state).replace(/_/g, " ")) + who
    + ". This request is decided, so it offers no buttons."));
  if (request.state === "changes_requested") {
    box.append(d.note ? el("p", "approval-feedback-text", "Feedback: " + d.note)
      : el("p", "hint", "No feedback was supplied with this earlier decision."));
  } else if (d.note) {
    box.append(el("p", "hint", "Note: " + d.note));
  }
  if (request.next_step) box.append(el("p", "hint approval-next", "Next: " + request.next_step));
  return box;
}

function approvalBlock(card) {
  const requests = card.approvals || [];
  if (!requests.length) return null;
  const box = el("div", "approvals");
  box.append(el("h4", null, "Approval requests"));
  if (approvalResult && approvalResult.task === card.id) {
    const result = el("div", "approval-result" + (approvalResult.ok ? "" : " is-error"));
    result.setAttribute("role", approvalResult.ok ? "status" : "alert");
    if (approvalResult.headline) result.append(el("strong", null, approvalResult.headline));
    result.append(el("p", null, approvalResult.text));
    box.append(result);
  }
  requests.slice().reverse().forEach((request) => {
    const key = draftKey(request);
    const item = el("div", "approval");
    item.dataset.request = request.id;
    item.dataset.state = request.effective_state;
    item.append(el("p", null, request.id + " · " + request.effective_state.replace(/_/g, " ") + " · "
      + request.action + " at gate " + request.gate_id + " · binding " + request.binding_token));
    item.append(el("p", "hint", "Scope: " + request.scope));
    item.append(el("p", "hint", "Summary: " + request.summary
      + (request.evidence && request.evidence.length ? " · Evidence: " + request.evidence.join(", ") : "")));
    if (request.effective_state !== "pending") delete approvalDrafts[key]; // no feedback UI left behind
    if (request.decision) item.append(decidedExplanation(request));
    if (request.effective_state === "stale") {
      item.append(el("p", "is-unknown", "Stale: the task or graph changed since this was requested. Reissue it; it cannot be decided."));
    }
    if (request.effective_state === "pending") item.append(...pendingControls(card, request, key));
    box.append(item);
  });
  return box;
}

/* Approve and Defer decide at once; Request Changes first opens a required feedback field and
 * decides only on Submit changes with 1-1000 characters of feedback. */
function pendingControls(card, request, key) {
  const busy = approvalBusy.has(key);
  const draft = approvalDrafts[key];
  const actions = el("div", "card-actions");
  const holder = el("div", "approval-feedback-holder");
  [["approve", "Approve"], ["request_changes", "Request Changes"], ["defer", "Defer"]].forEach(([decision, label]) => {
    const button = el("button", "ghost small decide", label);
    button.type = "button";
    button.dataset.decision = decision;
    button.disabled = busy;
    if (decision === "request_changes") {
      button.setAttribute("aria-expanded", draft && draft.open ? "true" : "false");
      // Opens the required feedback field in place; nothing is sent.
      button.addEventListener("click", () => {
        approvalDrafts[key] = { open: true, text: (approvalDrafts[key] || {}).text || "", error: null };
        button.setAttribute("aria-expanded", "true");
        const form = feedbackForm(card, request, key, approvalBusy.has(key), button);
        holder.replaceChildren(form);
        form.querySelector("textarea").focus();
      });
    } else {
      button.addEventListener("click", () => decide(card, request, decision, key));
    }
    actions.append(button);
  });
  const form = feedbackForm(card, request, key, busy, actions.querySelector('[data-decision="request_changes"]'));
  if (form) holder.append(form);
  return [actions, holder];
}

function feedbackForm(card, request, key, busy, opener) {
  const draft = approvalDrafts[key];
  if (!draft || !draft.open) return null;
  const form = el("div", "approval-feedback");
  const id = "feedback-" + request.id.replace(/[^A-Za-z0-9_-]/g, "_");
  const label = el("label", null, "What must change (required, at most " + FEEDBACK_LIMIT + " characters)");
  label.htmlFor = id;
  const area = el("textarea", "approval-note");
  area.id = id;
  area.rows = 4;
  area.required = true;
  area.maxLength = FEEDBACK_LIMIT;
  area.value = draft.text;
  const count = el("p", "hint approval-count", draft.text.length + " / " + FEEDBACK_LIMIT);
  const error = el("p", "approval-error is-error", draft.error || "");
  error.id = id + "-error";
  error.setAttribute("role", "alert");
  error.hidden = !draft.error;
  area.setAttribute("aria-describedby", error.id);
  area.addEventListener("input", () => {
    draft.text = area.value;
    count.textContent = area.value.length + " / " + FEEDBACK_LIMIT;
  });
  const submit = el("button", "small submit-changes", "Submit changes");
  submit.type = "button";
  submit.disabled = busy;
  submit.addEventListener("click", () => {
    const text = area.value.trim();
    draft.error = !text ? "Feedback is required: say what must change. Nothing was decided."
      : text.length > FEEDBACK_LIMIT ? "Feedback is too long (at most " + FEEDBACK_LIMIT + " characters). Nothing was decided."
        : null;
    if (draft.error) {
      error.textContent = draft.error;
      error.hidden = false;
      area.focus();
      return;
    }
    decide(card, request, "request_changes", key, text);
  });
  const cancel = el("button", "ghost small cancel-changes", "Cancel");
  cancel.type = "button";
  cancel.disabled = busy;
  cancel.addEventListener("click", () => {
    delete approvalDrafts[key];
    form.remove();
    if (opener) {
      opener.setAttribute("aria-expanded", "false");
      opener.focus();
    }
  });
  const buttons = el("div", "card-actions");
  buttons.append(submit, cancel);
  form.append(label, area, count, error, buttons);
  return form;
}

async function decide(card, request, decision, key, note) {
  if (approvalBusy.has(key)) return;
  approvalBusy.add(key);
  document.querySelectorAll(".approval").forEach((node) => {
    if (node.dataset.request === request.id) node.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  });
  // Exactly the request, task, gate, action and binding shown; feedback only with Request Changes.
  const body = { request_id: request.id, action: request.action, gate_id: request.gate_id,
    binding: request.binding_token, decision };
  if (decision === "request_changes") body.note = note;
  try {
    const result = await api("/api/tasks/" + card.id + "/approval/decide", { method: "POST", body: JSON.stringify(body) });
    delete approvalDrafts[key];
    approvalResult = { task: card.id, request: request.id, ok: true, headline: DECIDED_HEADLINES[decision],
      text: result.next_step };
  } catch (error) {
    approvalResult = { task: card.id, request: request.id, ok: false, headline: "Not decided", text: error.message };
  } finally {
    approvalBusy.delete(key);
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    await loadInspect();
  }
}

async function loadInspect() {
  if (inspectBusy || inspectId === null) return;
  // Do not redraw under someone typing an approval note.
  if (document.activeElement && document.activeElement.classList.contains("approval-note")) return;
  inspectBusy = true;
  const id = inspectId;
  const body = document.getElementById("inspect-body");
  const meta = document.getElementById("inspect-meta");
  try {
    const [obs, board] = await Promise.allSettled([api("/api/observability"), api("/api/board")]);
    if (id !== inspectId) return;
    showRunState(obsViews(obs));
    if (obs.status === "rejected" && body.dataset.task === id) {
      meta.textContent = "Could not refresh (" + obs.reason.message + "); showing the last loaded report.";
      return;
    }
    const views = obs.status === "fulfilled" && obs.value.available ? obs.value.tasks : [];
    const view = views.find((v) => String(v.task_id) === id) || null;
    const card = board.status === "fulfilled" ? board.value.cards.find((c) => String(c.id) === id) || null : null;
    // Keep opened run telemetry open across polls (matched by run id).
    const open = new Set([...body.querySelectorAll("details.run-telemetry[open]")].map((d) => d.dataset.runId));
    body.replaceChildren();
    delete body.dataset.task;
    if (view) {
      body.append(renderTaskObs(view));
      // The canonical record (details, relations, approval controls, history) as on a task without runs.
      if (card) body.querySelector(".obs-task").append(ticketRecord(card));
      body.dataset.task = id;
    } else if (card) {
      body.append(taskWithoutRuns(card, obs.status === "rejected" ? "Run records could not be loaded: " + obs.reason.message
        : obs.value.available ? "No runs recorded for this task yet." : "Run records are not available in this installation."));
    } else if (obs.status === "rejected") {
      body.append(el("p", "quiet", "Could not load this task: " + obs.reason.message));
    } else {
      body.append(el("p", "quiet", "Task #" + id + " was not found on the board or in run records."));
    }
    body.querySelectorAll("details.run-telemetry").forEach((d) => { d.open = open.has(d.dataset.runId); });
    meta.textContent = "Page refreshed " + new Date().toLocaleTimeString()
      + (obs.status === "fulfilled" && obs.value.runs_dir ? " · records: " + obs.value.runs_dir : "");
  } finally {
    inspectBusy = false;
  }
}

document.getElementById("inspect-back").addEventListener("click", leaveInspect);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && inspectId !== null && !dialog.open) {
    event.preventDefault();
    leaveInspect();
  }
});

/* #inspect/<id> opens Inspect; #kanban the board; anything else Observability.
 * Leaving Inspect puts focus back on the link that opened it, else the tab. */
function route() {
  const match = /^#inspect\/([A-Za-z0-9._-]+)$/.exec(location.hash);
  if (match) return showInspect(match[1]);
  const left = inspectId;
  const tab = location.hash === "#kanban" ? "kanban" : "observability";
  return Promise.resolve(showTab(tab)).then(() => {
    if (left === null) return;
    const panel = document.getElementById(TABS[tab]);
    (panel.querySelector('[data-inspect="' + left + '"]') || document.getElementById("tab-" + tab)).focus();
  });
}

// Polling only reads; the Tasks board refresh is slower and pauses while the editor is open.
let ticks = 0;
setInterval(() => {
  ticks += 1;
  if (document.hidden) return;
  const live = document.getElementById("obs-live").checked;
  // Weekly allowance on every view and every tick: a cheap local GET, so a new Claude status-line
  // snapshot shows within 3 s. The server's worker alone reads Codex, once a minute.
  if (live) loadQuota();
  if (inspectId !== null) {
    if (live) loadInspect();
  } else if (activeTab === "observability" && live) {
    loadObservability();
  }
  else if (activeTab === "kanban" && kanbanLoaded && ticks % 5 === 0) {
    refreshRunState();
    if (!dialog.open) load("Refreshing…");
  }
}, 3000);

document.getElementById("obs-live").addEventListener("change", (event) => {
  if (!event.target.checked) return showRunState(null);
  loadQuota();
  if (inspectId !== null) loadInspect();
  else if (activeTab === "observability") loadObservability();
  else refreshRunState();
});

// Observability is the default view; #kanban opens the board, #inspect/<id> a task.
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && document.getElementById("obs-live").checked && quotaDue()) loadQuota();
});
route();
if (quotaDue()) loadQuota(); // at startup on any view, Tasks included
